import torch
from scene import Scene
import os
from os import makedirs
from gaussian_renderer import render, integrate, integrate_sdf
import random
from tqdm import tqdm
from argparse import ArgumentParser
from arguments import ModelParams, OptimizationParams, PipelineParams, get_combined_args
# from gaussian_renderer import GaussianModel
from scene.sdf_gaussian_model_v3 import GaussianModel
import numpy as np
import trimesh
from tetranerf.utils.extension import cpp
from utils.tetmesh import marching_tetrahedra
from skimage.measure import marching_cubes

@torch.no_grad()
def filter_points_in_bounding_box(points, bounding_box):
    bbox_min = bounding_box[:, 0]
    bbox_max = bounding_box[:, 1]
    
    mask = (points >= bbox_min) & (points <= bbox_max)
    mask = mask.any(dim=1)
    
    filtered_points = points[mask]
    
    return filtered_points, mask

@torch.no_grad()
def marching_tetrahedra_with_binary_search(model_path, name, iteration, gaussians):
    render_path = os.path.join(model_path, name, "ours_{}".format(iteration), "fusion")

    makedirs(render_path, exist_ok=True)
    
    # generate tetra points here
    points, points_scale = gaussians.get_tetra_points()
    points, mask = filter_points_in_bounding_box(points, gaussians.bounding_box)
    points_scale = points_scale[mask]
    # load cell if exists
    if False: #os.path.exists(os.path.join(render_path, "cells.pt")):
        print("load existing cells")
        cells = torch.load(os.path.join(render_path, "cells.pt"))
    else:
        # create cell and save cells
        print("create cells and save")
        cells = cpp.triangulate(points)
        # we should filter the cell if it is larger than the gaussians
        torch.save(cells, os.path.join(render_path, "cells.pt"))

    sdf = gaussians.query_sdf(points.cuda())['sdf']
    sdf = sdf[None]

    vertices = points.cuda()[None]
    tets = cells.cuda().long()
    print(vertices.shape, tets.shape)

    torch.cuda.empty_cache()
    verts_list, scale_list, faces_list, _ = marching_tetrahedra(vertices, tets, sdf, points_scale[None])
    torch.cuda.empty_cache()
    
    end_points, end_sdf = verts_list[0]
    end_scales = scale_list[0]
    
    faces=faces_list[0].cpu().numpy()
    points = (end_points[:, 0, :] + end_points[:, 1, :]) / 2.
        
    left_points = end_points[:, 0, :]
    right_points = end_points[:, 1, :]
    left_sdf = end_sdf[:, 0, :]
    right_sdf = end_sdf[:, 1, :]
    left_scale = end_scales[:, 0, 0]
    right_scale = end_scales[:, 1, 0]
    distance = torch.norm(left_points - right_points, dim=-1)
    scale = left_scale + right_scale
    
    n_binary_steps = 8
    for step in range(n_binary_steps):
        print("binary search in step {}".format(step))
        mid_points = (left_points + right_points) / 2
        mid_sdf = gaussians.query_sdf(mid_points.cuda())['sdf']
        mid_sdf = mid_sdf.squeeze().unsqueeze(-1)

        ind_low = ((mid_sdf < 0) & (left_sdf < 0)) | ((mid_sdf > 0) & (left_sdf > 0))

        left_sdf[ind_low] = mid_sdf[ind_low]
        right_sdf[~ind_low] = mid_sdf[~ind_low]
        left_points[ind_low.flatten()] = mid_points[ind_low.flatten()]
        right_points[~ind_low.flatten()] = mid_points[~ind_low.flatten()]
    
        points = (left_points + right_points) / 2
        if step not in [7]:
            continue
        
        mesh = trimesh.Trimesh(vertices=points.cpu().numpy(), faces=faces, process=False)
        
        # filter
        mask = (distance <= scale).cpu().numpy()
        face_mask = mask[faces].all(axis=1)
        mesh.update_vertices(mask)
        mesh.update_faces(face_mask)

        mesh.export(os.path.join(render_path, f"mesh_binary_search_{step}.ply"))

    # linear interpolation
    # right_sdf *= -1
    # points = (left_points * left_sdf + right_points * right_sdf) / (left_sdf + right_sdf)
    # mesh = trimesh.Trimesh(vertices=points.cpu().numpy(), faces=faces)
    # mesh.export(os.path.join(render_path, f"mesh_binary_search_interp.ply"))
    

def extract_mesh(dataset : ModelParams, opt, iteration : int, pipeline : PipelineParams):
    with torch.no_grad():
        gaussians = GaussianModel(dataset.sh_degree, opt.network)
        gaussians.load_ply(os.path.join(dataset.model_path, "point_cloud", f"iteration_{iteration}", "point_cloud.ply"))
        gaussians.load_model(os.path.join(dataset.model_path, "point_cloud", f"iteration_{iteration}", "model.pt"))
        
        marching_tetrahedra_with_binary_search(dataset.model_path, "test", iteration, gaussians)

if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Testing script parameters")
    model = ModelParams(parser, sentinel=True)
    op = OptimizationParams(parser)
    pipeline = PipelineParams(parser)
    parser.add_argument("--iteration", default=30000, type=int)
    parser.add_argument("--quiet", action="store_true")
    args = get_combined_args(parser)

    print("Rendering " + args.model_path)

    random.seed(0)
    np.random.seed(0)
    torch.manual_seed(0)
    torch.cuda.set_device(torch.device("cuda:0"))
    
    extract_mesh(model.extract(args), op.extract(args), args.iteration, pipeline.extract(args))