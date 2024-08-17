import torch
import numpy as np
from utils.general_utils import inverse_sigmoid, get_expon_lr_func, build_rotation
from torch import nn
import os
from utils.system_utils import mkdir_p
from plyfile import PlyData, PlyElement
from utils.sh_utils import RGB2SH
from simple_knn._C import distCUDA2
from scipy.ndimage import gaussian_filter, distance_transform_edt
from utils.graphics_utils import BasicPointCloud
from utils.general_utils import strip_symmetric, build_scaling_rotation, get_minimum_axis, get_sorted_axis
import torch.nn.functional as F
import trimesh
from utils.vis_utils import save_points
from utils.network_utils import LaplaceDensity, SimpleSDF
from scene.appearance_network import AppearanceNetwork
from scipy.spatial import cKDTree
import open3d as o3d
import matplotlib.pyplot as plt

class GaussianModel:

    def setup_functions(self):
        def build_covariance_from_scaling_rotation(scaling, scaling_modifier, rotation):
            L = build_scaling_rotation(scaling_modifier * scaling, rotation)
            actual_covariance = L @ L.transpose(1, 2)
            symm = strip_symmetric(actual_covariance)
            return symm
        
        self.scaling_activation = torch.exp
        self.scaling_inverse_activation = torch.log

        self.covariance_activation = build_covariance_from_scaling_rotation

        # self.opacity_activation = torch.sigmoid
        # self.inverse_opacity_activation = inverse_sigmoid
        
        self.rotation_activation = torch.nn.functional.normalize


    def __init__(self, sh_degree : int, cfg):
        self.cfg = cfg
        self.active_sh_degree = 3
        self.max_sh_degree = sh_degree  
        self._xyz = torch.empty(0)
        self._features_dc = torch.empty(0)
        self._features_rest = torch.empty(0)
        self.max_radii2D = torch.empty(0)
        self.xyz_gradient_accum = torch.empty(0)
        self.denom = torch.empty(0)
        self.optimizer = None
        self.percent_dense = 0
        self.spatial_lr_scale = 0
        self.vox_size = None
        self.bounding_box = None
        self.setup_functions()
        self.count = 0
        

    def set_bbox(self, enlarge=1.1):
        '''
        Get bounding box of current scene, enlarge a little bit.
        '''
        with torch.no_grad():       
            min_values, _ = torch.min(self.get_xyz, dim=0)
            max_values, _ = torch.max(self.get_xyz, dim=0)

            min_point = min_values * enlarge
            max_point = max_values * enlarge

            self.bounding_box = torch.stack([min_point, max_point], dim=-1)

            # print(f"Corrected number of points: {self.get_xyz.shape[0]}")
            print(f"Bounding box: {min_point.cpu().numpy()}, {max_point.cpu().numpy()}")


    def capture(self):
        return (
            self.active_sh_degree,
            self._xyz,
            self._features_dc,
            self._features_rest,
            self.max_radii2D,
            self.xyz_gradient_accum,
            self.denom,
            self.optimizer.state_dict(),
            self.network_optimizer.state_dict(),
            self.spatial_lr_scale
        )
    
    def restore(self, model_args, training_args):
        (self.active_sh_degree, 
        self._xyz, 
        self._features_dc, 
        self._features_rest,
        self.max_radii2D, 
        xyz_gradient_accum, 
        denom,
        opt_dict, 
        net_opt_dict,
        self.spatial_lr_scale) = model_args
        self.training_setup(training_args)
        self.xyz_gradient_accum = xyz_gradient_accum
        self.denom = denom
        self.optimizer.load_state_dict(opt_dict)
        self.network_optimizer.load_state_dict(net_opt_dict)

    @property
    def get_scaling(self):
        return self.scaling_activation(self._scaling)
    
    @property
    def get_scaling_with_3D_filter(self):
        scales = self.get_scaling
        
        scales = torch.square(scales) + torch.square(self.filter_3D)
        scales = torch.sqrt(scales)
        return scales
    
    @property
    def get_rotation(self):
        return self.rotation_activation(self._rotation)
    
    @property
    def get_xyz(self):
        return self._xyz
    
    @property
    def get_features(self):
        features_dc = self._features_dc
        features_rest = self._features_rest
        return torch.cat((features_dc, features_rest), dim=1)
    
    # @property
    # def get_opacity(self):
    #     # sdf = self.query_sdf(self._xyz)
    #     # return self.opacity_activation(sdf)
    #     return self.opacity_activation(self._sdf)
    
    @property
    def get_opacity_with_3D_filter(self):
        sdf = self.query_sdf(self._xyz)
        opacity = self.opacity_activation(sdf)
        # apply 3D filter
        scales = self.get_scaling
        
        scales_square = torch.square(scales)
        det1 = scales_square.prod(dim=1)
        
        scales_after_square = scales_square + torch.square(self.filter_3D) 
        det2 = scales_after_square.prod(dim=1) 
        coef = torch.sqrt(det1 / det2)
        return opacity * coef[..., None]
    
    # def get_opacity(self, reset=False):
    #     sdf = self.query_sdf(self._xyz)
    #     density = self.opacity_activation(sdf)
    #     dists, indices = self.query_kdtree(points=self._xyz, query_points=self._xyz, k=10, reset=reset)


    #     return opacity
    
    def query_kdtree(self, points=None, query_points=None, k=10, reset=False):
        if reset:
            self.tree = cKDTree(points)
        dists, indices = self.tree.query(query_points, k=k)
        return dists, indices
    
    def get_apperance_embedding(self, idx):
        return self._appearance_embeddings[idx]
    
    def get_sorted_axis(self):
        return get_sorted_axis(self.get_scaling, self.get_rotation)
    
    def get_covariance(self, scaling_modifier = 1):
        return self.covariance_activation(self.get_scaling, scaling_modifier, self._rotation)

    def get_view2gaussian(self, viewmatrix):
        r = self._rotation
        norm = torch.sqrt(r[:,0]*r[:,0] + r[:,1]*r[:,1] + r[:,2]*r[:,2] + r[:,3]*r[:,3])

        q = r / norm[:, None]
        
        R = torch.zeros((q.size(0), 3, 3), device='cuda')

        r = q[:, 0]
        x = q[:, 1]
        y = q[:, 2]
        z = q[:, 3]

        R[:, 0, 0] = 1 - 2 * (y*y + z*z)
        R[:, 0, 1] = 2 * (x*y - r*z)
        R[:, 0, 2] = 2 * (x*z + r*y)
        R[:, 1, 0] = 2 * (x*y + r*z)
        R[:, 1, 1] = 1 - 2 * (x*x + z*z)
        R[:, 1, 2] = 2 * (y*z - r*x)
        R[:, 2, 0] = 2 * (x*z - r*y)
        R[:, 2, 1] = 2 * (y*z + r*x)
        R[:, 2, 2] = 1 - 2 * (x*x + y*y)
    
        rots = R
        xyz = self.get_xyz
        N = xyz.shape[0]
        G2W = torch.zeros((N, 4, 4), device='cuda')
        G2W[:, :3, :3] = rots # TODO check if we need to transpose here
        G2W[:, :3, 3] = xyz
        G2W[:, 3, 3] = 1.0
        
        viewmatrix = viewmatrix.transpose(0, 1)
        G2V = viewmatrix @ G2W
        
        R = G2V[:, :3, :3]
        t = G2V[:, :3, 3]
        
        t2 = torch.bmm(-R.transpose(1, 2), t[..., None])[..., 0]
        V2G = torch.zeros((N, 4, 4), device='cuda')
        V2G[:, :3, :3] = R.transpose(1, 2)
        V2G[:, :3, 3] = t2
        V2G[:, 3, 3] = 1.0
        
        # transpose view2gaussian to match glm in CUDA code
        V2G = V2G.transpose(2, 1).contiguous()
        return V2G

    def oneupSHdegree(self):
        if self.active_sh_degree < self.max_sh_degree:
            self.active_sh_degree += 1

    def create_from_pcd(self, pcd : BasicPointCloud, spatial_lr_scale : float):
        self.spatial_lr_scale = spatial_lr_scale
        fused_point_cloud = torch.tensor(np.asarray(pcd.points)).float().cuda()
        fused_color = RGB2SH(torch.tensor(np.asarray(pcd.colors)).float().cuda())
        features = torch.zeros((fused_color.shape[0], 3, (self.max_sh_degree + 1) ** 2)).float().cuda()
        features[:, :3, 0 ] = fused_color
        features[:, 3:, 1:] = 0.0

        print("Number of points at initialisation : ", fused_point_cloud.shape[0])

        self._xyz = nn.Parameter(fused_point_cloud.requires_grad_(True))
        self._features_dc = nn.Parameter(features[:,:,0:1].transpose(1, 2).contiguous().requires_grad_(True))
        self._features_rest = nn.Parameter(features[:,:,1:].transpose(1, 2).contiguous().requires_grad_(True))
        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")

    def training_setup(self, training_args):
        if self.bounding_box is None:
            self.set_bbox()

        self.percent_dense = training_args.percent_dense
        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.xyz_gradient_accum_abs = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.xyz_gradient_accum_abs_max = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.view_mask = torch.zeros(self.get_xyz.shape[0], dtype=torch.bool, device="cuda")
        self.query_sdf = SimpleSDF(self.cfg, self.bounding_box, in_dim=3, hidden_dim=32).cuda()

        l = [
            {'params': [self._xyz], 'lr': training_args.position_lr_init * self.spatial_lr_scale, "name": "xyz"},
            {'params': [self._features_dc], 'lr': training_args.feature_lr, "name": "f_dc"},
            {'params': [self._features_rest], 'lr': training_args.feature_lr / 20.0, "name": "f_rest"},
        ]

        self.optimizer = torch.optim.Adam(l, lr=0.0, eps=1e-15)
        self.xyz_scheduler_args = get_expon_lr_func(lr_init=training_args.position_lr_init*self.spatial_lr_scale,
                                                    lr_final=training_args.position_lr_final*self.spatial_lr_scale,
                                                    lr_delay_mult=training_args.position_lr_delay_mult,
                                                    max_steps=training_args.position_lr_max_steps)
        
        sdf_l = [{'params': list(self.query_sdf.parameters()),'lr': training_args.network_lr, "name": "sdf"}]
        self.network_optimizer = torch.optim.Adam(sdf_l, lr=0.0, eps=1e-15)


    def update_learning_rate(self, iteration):
        ''' Learning rate scheduling per step '''
        for param_group in self.optimizer.param_groups:
            if param_group["name"] == "xyz":
                lr = self.xyz_scheduler_args(iteration)
                param_group['lr'] = lr

    def construct_list_of_attributes(self, exclude_filter=False):
        l = ['x', 'y', 'z', 'nx', 'ny', 'nz']
        # All channels except the 3 DC
        for i in range(self._features_dc.shape[1]*self._features_dc.shape[2]):
            l.append('f_dc_{}'.format(i))
        for i in range(self._features_rest.shape[1]*self._features_rest.shape[2]):
            l.append('f_rest_{}'.format(i))

        return l

    def save_ply(self, path):
        mkdir_p(os.path.dirname(path))

        xyz = self._xyz.detach().cpu().numpy()
        normals = np.zeros_like(xyz)
        f_dc = self._features_dc.detach().transpose(1, 2).flatten(start_dim=1).contiguous().cpu().numpy()
        f_rest = self._features_rest.detach().transpose(1, 2).flatten(start_dim=1).contiguous().cpu().numpy()

        # filter_3D = self.filter_3D.detach().cpu().numpy()
        dtype_full = [(attribute, 'f4') for attribute in self.construct_list_of_attributes()]

        elements = np.empty(xyz.shape[0], dtype=dtype_full)
        attributes = np.concatenate((xyz, normals, f_dc, f_rest), axis=1)
        elements[:] = list(map(tuple, attributes))
        el = PlyElement.describe(elements, 'vertex')
        PlyData([el]).write(path)

    def save_model(self, path):
        save_dict = {'query_sdf': self.query_sdf.state_dict(),
                     'bounding_box': self.bounding_box}
        torch.save(save_dict, path)
        print('Model saved.')

    def load_model(self, path):
        model_dict = torch.load(path)
        self.bounding_box = model_dict['bounding_box']
        self.query_sdf = SimpleSDF(self.cfg, self.bounding_box, in_dim=3, hidden_dim=32).cuda()
        self.query_sdf.load_state_dict(model_dict['query_sdf'])


    def save_fused_ply(self, path):
        mkdir_p(os.path.dirname(path))

        xyz = self._xyz.detach().cpu().numpy()
        normals = np.zeros_like(xyz)
        f_dc = self._features_dc.detach().transpose(1, 2).flatten(start_dim=1).contiguous().cpu().numpy()
        f_rest = self._features_rest.detach().transpose(1, 2).flatten(start_dim=1).contiguous().cpu().numpy()

        dtype_full = [(attribute, 'f4') for attribute in self.construct_list_of_attributes(exclude_filter=True)]

        elements = np.empty(xyz.shape[0], dtype=dtype_full)
        attributes = np.concatenate((xyz, normals, f_dc, f_rest), axis=1)
        elements[:] = list(map(tuple, attributes))
        el = PlyElement.describe(elements, 'vertex')
        PlyData([el]).write(path)
    
    @torch.no_grad()
    def get_tetra_points(self):
        M = trimesh.creation.box()
        M.vertices *= 2
        
        xyz = self.get_xyz
        # scale = self.get_scaling_with_3D_filter * 3. # TODO test
        out = self.query_sdf(self.get_xyz, return_opacity=True, return_rot_scale=True)
        scale = out['scale']
        self.rot = out['rot']
        rots = build_rotation(self.rot)
        # filter points with small opacity for bicycle scene
        # opacity = self.get_opacity_with_3D_filter
        # mask = (opacity > 0.1).squeeze(-1)
        # xyz = xyz[mask]
        # scale = scale[mask]
        # rots = rots[mask]
        
        vertices = M.vertices.T    
        vertices = torch.from_numpy(vertices).float().cuda().unsqueeze(0).repeat(xyz.shape[0], 1, 1)
        # scale vertices first
        vertices = vertices * scale.unsqueeze(-1)
        vertices = torch.bmm(rots, vertices).squeeze(-1) + xyz.unsqueeze(-1)
        vertices = vertices.permute(0, 2, 1).reshape(-1, 3).contiguous()
        # concat center points
        vertices = torch.cat([vertices, xyz], dim=0)
        
        # scale is not a good solution but use it for now
        scale = scale.max(dim=-1, keepdim=True)[0]
        scale_corner = scale.repeat(1, 8).reshape(-1, 1)
        vertices_scale = torch.cat([scale_corner, scale], dim=0)
        
        return vertices, vertices_scale
    
    # def reset_sdf(self):
    #     self.cfg['density']['params_init']['alpha'] = 0.02
    #     self.sdf2opacity = LaplaceDensity(**self.cfg['density']).cuda()
    #     sdf_l = [{'params': self.sdf2opacity.parameters(), 'lr': self.sdf2opacity_lr, "name": "beta"}]
    #     self.network_optimizer = torch.optim.Adam(sdf_l, lr=0.0, eps=1e-15)
    #     self.opacity_activation = self.sdf2opacity.density_func

    def load_ply(self, path):
        plydata = PlyData.read(path)

        xyz = np.stack((np.asarray(plydata.elements[0]["x"]),
                        np.asarray(plydata.elements[0]["y"]),
                        np.asarray(plydata.elements[0]["z"])),  axis=1)
        
        features_dc = np.zeros((xyz.shape[0], 3, 1))
        features_dc[:, 0, 0] = np.asarray(plydata.elements[0]["f_dc_0"])
        features_dc[:, 1, 0] = np.asarray(plydata.elements[0]["f_dc_1"])
        features_dc[:, 2, 0] = np.asarray(plydata.elements[0]["f_dc_2"])

        extra_f_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("f_rest_")]
        extra_f_names = sorted(extra_f_names, key = lambda x: int(x.split('_')[-1]))
        assert len(extra_f_names)==3*(self.max_sh_degree + 1) ** 2 - 3
        features_extra = np.zeros((xyz.shape[0], len(extra_f_names)))
        for idx, attr_name in enumerate(extra_f_names):
            features_extra[:, idx] = np.asarray(plydata.elements[0][attr_name])
        # Reshape (P,F*SH_coeffs) to (P, F, SH_coeffs except DC)
        features_extra = features_extra.reshape((features_extra.shape[0], 3, (self.max_sh_degree + 1) ** 2 - 1))

        self._xyz = nn.Parameter(torch.tensor(xyz, dtype=torch.float, device="cuda").requires_grad_(True))
        self._features_dc = nn.Parameter(torch.tensor(features_dc, dtype=torch.float, device="cuda").transpose(1, 2).contiguous().requires_grad_(True))
        self._features_rest = nn.Parameter(torch.tensor(features_extra, dtype=torch.float, device="cuda").transpose(1, 2).contiguous().requires_grad_(True))

        self.active_sh_degree = self.max_sh_degree

    def replace_tensor_to_optimizer(self, tensor, name):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] in ["appearance_embeddings", "appearance_network", "beta"]:
                continue
            if group["name"] == name:
                stored_state = self.optimizer.state.get(group['params'][0], None)
                stored_state["exp_avg"] = torch.zeros_like(tensor)
                stored_state["exp_avg_sq"] = torch.zeros_like(tensor)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(tensor.requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def _prune_optimizer(self, mask):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] in ["appearance_embeddings", "appearance_network", "beta", "sdf"]:
                continue
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:
                stored_state["exp_avg"] = stored_state["exp_avg"][mask]
                stored_state["exp_avg_sq"] = stored_state["exp_avg_sq"][mask]

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter((group["params"][0][mask].requires_grad_(True)))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(group["params"][0][mask].requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def prune_points(self, mask):
        valid_points_mask = ~mask
        optimizable_tensors = self._prune_optimizer(valid_points_mask)

        self._xyz = optimizable_tensors["xyz"]
        self._features_dc = optimizable_tensors["f_dc"]
        self._features_rest = optimizable_tensors["f_rest"]
        self.xyz_gradient_accum = self.xyz_gradient_accum[valid_points_mask]
        self.xyz_gradient_accum_abs = self.xyz_gradient_accum_abs[valid_points_mask]
        self.xyz_gradient_accum_abs_max = self.xyz_gradient_accum_abs_max[valid_points_mask]
        self.denom = self.denom[valid_points_mask]
        self.max_radii2D = self.max_radii2D[valid_points_mask]

    def cat_tensors_to_optimizer(self, tensors_dict):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] in ["appearance_embeddings", "appearance_network", "beta", "sdf"]:
                continue
            assert len(group["params"]) == 1
            extension_tensor = tensors_dict[group["name"]]
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:

                stored_state["exp_avg"] = torch.cat((stored_state["exp_avg"], torch.zeros_like(extension_tensor)), dim=0)
                stored_state["exp_avg_sq"] = torch.cat((stored_state["exp_avg_sq"], torch.zeros_like(extension_tensor)), dim=0)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]

        return optimizable_tensors

    def densification_postfix(self, new_xyz, new_features_dc, new_features_rest):
        d = {"xyz": new_xyz,
             "f_dc": new_features_dc,
             "f_rest": new_features_rest,}

        optimizable_tensors = self.cat_tensors_to_optimizer(d)
        self._xyz = optimizable_tensors["xyz"]
        self._features_dc = optimizable_tensors["f_dc"]
        self._features_rest = optimizable_tensors["f_rest"]

        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.xyz_gradient_accum_abs = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.xyz_gradient_accum_abs_max = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        # self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")

    def densify_and_split(self, grads, grad_threshold, grads_abs, grad_abs_threshold, scene_extent, N=2, use_sdf_mask=False):
        n_init_points = self.get_xyz.shape[0]
        # Extract points that satisfy the gradient condition
        padded_grad = torch.zeros((n_init_points), device="cuda")
        padded_grad[:grads.shape[0]] = grads.squeeze()
        selected_pts_mask = torch.where((padded_grad) >= grad_threshold, True, False)
        padded_grad_abs = torch.zeros((n_init_points), device="cuda")
        padded_grad_abs[:grads_abs.shape[0]] = grads_abs.squeeze()
        selected_pts_mask_abs = torch.where(padded_grad_abs >= grad_abs_threshold, True, False)
        selected_pts_mask = torch.logical_or(selected_pts_mask, selected_pts_mask_abs)
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(self.scale, dim=1).values > self.percent_dense*scene_extent)

        if use_sdf_mask:
            sdf_mask = self.sdf <= 0.
            selected_pts_mask = torch.logical_and(selected_pts_mask, sdf_mask)

        stds = self.scale[selected_pts_mask].repeat(N,1)
        means =torch.zeros((stds.size(0), 3),device="cuda")
        samples = torch.normal(mean=means, std=stds)
        rots = build_rotation(self.rot[selected_pts_mask]).repeat(N,1,1)
        new_xyz = torch.bmm(rots, samples.unsqueeze(-1)).squeeze(-1) + self.get_xyz[selected_pts_mask].repeat(N, 1)
        new_features_dc = self._features_dc[selected_pts_mask].repeat(N,1,1)
        new_features_rest = self._features_rest[selected_pts_mask].repeat(N,1,1)
        self.densification_postfix(new_xyz, new_features_dc, new_features_rest)

        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")
        prune_filter = torch.cat((selected_pts_mask, torch.zeros(N * selected_pts_mask.sum(), device="cuda", dtype=bool)))
        self.prune_points(prune_filter)

    def densify_and_clone(self, grads, grad_threshold, grads_abs, grad_abs_threshold, scene_extent, use_sdf_mask=False):
        # Extract points that satisfy the gradient condition
        selected_pts_mask = torch.where((torch.norm(grads, dim=-1))>= grad_threshold, True, False)
        selected_pts_mask_abs = torch.where(torch.norm(grads_abs, dim=-1) >= grad_abs_threshold, True, False)
        selected_pts_mask = torch.logical_or(selected_pts_mask, selected_pts_mask_abs)
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(self.scale, dim=1).values <= self.percent_dense*scene_extent)
        if use_sdf_mask:
            sdf_mask = self.sdf <= 0.
            selected_pts_mask = torch.logical_and(selected_pts_mask, sdf_mask)
        
        new_xyz = self._xyz[selected_pts_mask]
        # sample a new gaussian instead of fixing position
        stds = self.scale[selected_pts_mask]
        means =torch.zeros((stds.size(0), 3),device="cuda")
        samples = torch.normal(mean=means, std=stds)
        rots = build_rotation(self.rot[selected_pts_mask])
        new_xyz = torch.bmm(rots, samples.unsqueeze(-1)).squeeze(-1) + self.get_xyz[selected_pts_mask]   
        new_features_dc = self._features_dc[selected_pts_mask]
        new_features_rest = self._features_rest[selected_pts_mask]

        self.densification_postfix(new_xyz, new_features_dc, new_features_rest)
        self.scale = torch.cat((self.scale, self.scale[selected_pts_mask]), dim=0)
        self.rot = torch.cat((self.rot, self.rot[selected_pts_mask]), dim=0)

        self.max_radii2D = torch.cat((self.max_radii2D, self.max_radii2D[selected_pts_mask]), dim=0)
        if use_sdf_mask:
            self.sdf = torch.cat((self.sdf, self.sdf[selected_pts_mask]), dim=0)


    def densify_and_prune(self, max_grad, min_opacity, extent, max_screen_size, iteration=None, z_prune=False):
        out = self.query_sdf(self.get_xyz, return_opacity=True, return_rot_scale=True)
        self.sdf = out['sdf'].squeeze().detach()
        self.opacity = out['opacity'].squeeze().detach()
        self.scale = out['scale'].detach()
        self.rot = out['rot'].detach()
        prune_mask = (self.opacity < min_opacity).squeeze()

        if max_screen_size:
            big_points_vs = (self.max_radii2D*0) > max_screen_size
            big_points_ws = self.get_scaling.max(dim=1).values > 0.1 * extent
            prune_mask = torch.logical_or(torch.logical_or(prune_mask, big_points_vs), big_points_ws)
            
        self.prune_points(prune_mask)
        prune = self._xyz.shape[0]
        self.sdf = self.sdf[~prune_mask]
        self.scale = self.scale[~prune_mask]
        self.rot = self.rot[~prune_mask]

        grads = self.xyz_gradient_accum / self.denom
        grads[grads.isnan()] = 0.0
        
        grads_abs = self.xyz_gradient_accum_abs / self.denom
        grads_abs[grads_abs.isnan()] = 0.0
        ratio = (torch.norm(grads, dim=-1) >= max_grad).float().mean()
        Q = torch.quantile(grads_abs.reshape(-1), 1 - ratio)
        
        before = self._xyz.shape[0]
        
        self.densify_and_clone(grads, max_grad, grads_abs, Q, extent, use_sdf_mask=False)
        clone = self._xyz.shape[0]
        
        self.densify_and_split(grads, max_grad, grads_abs, Q, extent, use_sdf_mask=False)
        split = self._xyz.shape[0]

        self.view_mask = torch.zeros(self.get_xyz.shape[0], dtype=torch.bool, device="cuda")

        torch.cuda.empty_cache()
        return clone - before, split - clone, split - prune

    def add_densification_stats(self, viewspace_point_tensor, update_filter, frustum_mask):
        self.xyz_gradient_accum[update_filter] += torch.norm(viewspace_point_tensor.grad[update_filter,:2], dim=-1, keepdim=True)
        #TODO maybe use max instead of average
        self.xyz_gradient_accum_abs[update_filter] += torch.norm(viewspace_point_tensor.grad[update_filter,2:], dim=-1, keepdim=True)
        self.xyz_gradient_accum_abs_max[update_filter] = torch.max(self.xyz_gradient_accum_abs_max[update_filter], torch.norm(viewspace_point_tensor.grad[update_filter,2:], dim=-1, keepdim=True))
        self.denom[update_filter] += 1
        self.view_mask[frustum_mask] = True