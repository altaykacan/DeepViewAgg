from abc import ABC

import torch
import torch.nn as nn
from torch_points3d.core.multimodal.data import MODALITY_NAMES
from torch_points3d.core.common_modules.base_modules import Identity
from torchsparse.nn.functional import sphash, sphashquery
import torch_scatter

try:
    import MinkowskiEngine as me
except:
    me = None
try:
    import torchsparse as ts
except:
    ts = None


class MultimodalBlockDown(nn.Module, ABC):
    """Multimodal block with downsampling that looks like:

                 -- 3D Conv ---- Merge i -- 3D Conv --
    MMData IN          ...        |                       MMData OUT
                 -- Mod i Conv --|--------------------
                       ...
    """

    def __init__(self, down_block, conv_block, **kwargs):
        """Build the Multimodal module from already-instantiated
        modules. Modality-specific modules are expected to be passed in
        dictionaries holding fully-fledged UnimodalBranch modules.
        """
        # BaseModule initialization
        super(MultimodalBlockDown, self).__init__()

        # Blocks for the implicitly main modality: 3D
        self.down_block = down_block if down_block is not None else Identity()
        self.conv_block = conv_block if conv_block is not None else Identity()

        # Initialize the dict holding the conv and merge blocks for all
        # modalities
        self._modalities = []
        self._init_from_kwargs(**kwargs)

        # Expose the 3D down_conv .sampler attribute (for
        # UnwrappedUnetBasedModel)
        # TODO this is for KPConv, is it doing the intended, is it
        #  needed at all ?
        self.sampler = [getattr(self.down_block, "sampler", None),
                        getattr(self.conv_block, "sampler", None)]

    def _init_from_kwargs(self, **kwargs):
        """Kwargs are expected to carry fully-fledged modality-specific
        UnimodalBranch modules.
        """
        for m in kwargs.keys():
            assert (m in MODALITY_NAMES), \
                f"Invalid kwarg modality '{m}', expected one of " \
                f"{MODALITY_NAMES}."
            assert isinstance(kwargs[m], (UnimodalBranch, nn.Identity)), \
                f"Expected a UnimodalBranch module for '{m}' modality " \
                f"but got {type(kwargs[m])} instead."
            setattr(self, m, kwargs[m])
            self._modalities.append(m)

    @property
    def modalities(self):
        return self._modalities

    @property
    def num_modalities(self):
        return len(self.modalities) + 1

    def forward(self, mm_data_tuple):
        """
        Forward pass of the MultiModalBlockDown.

        Expects a tuple of 3D data (Data, SparseTensor, etc.) destined
        for the 3D convolutional modules, and a dictionary of
        modality-specific data equipped with corresponding mappings.
        """
        # Unpack the multimodal data tuple
        x_3d, x_seen, mod_dict = mm_data_tuple

        # Conv on the main 3D modality - assumed to reduce 3D resolution
        x_3d, x_seen, mod_dict = self.forward_3d_block_down(
            x_3d, x_seen, mod_dict, self.down_block)

        for m in self.modalities:
            mod_branch = getattr(self, m)
            x_3d, x_seen_mod, mod_dict[m] = mod_branch((x_3d, x_seen, mod_dict[m]))
            if x_seen is None:
                x_seen = x_seen_mod
            else:
                x_seen = torch.logical_or(x_seen, x_seen_mod)

        # Conv on the main 3D modality
        x_3d, x_seen, mod_dict = self.forward_3d_block_down(
            x_3d, x_seen, mod_dict, self.conv_block)

        return tuple((x_3d, x_seen, mod_dict))

    @staticmethod
    def forward_3d_block_down(x_3d, x_seen, mod_dict, block):
        """
        Wrapper method to apply the forward pass on a 3D down conv
        block while preserving modality-specific mappings.

        This both runs the forward method of the input block but also
        catches the reindexing scheme, in case a sampling or sparse
        strided convolution is applied in the 3D conv block.

        For MinkowskiEngine or TorchSparse sparse tensors, the
        reindexing is recovered from the input/output coordinates. If
        no strided convolution was applied, the indexing stays the same
        and a None index is returned. Otherwise, the returned index
        maps indices as follows: i -> idx[i].

        For non-sparse convolutions, the reindexing is carried by the
        sampler's 'last_index' attribute. If no sampling was applied,
        the indexing stays the same and a None index is returned.
        Otherwise, the returned index carries the indices of the
        selected points with respect to their input order.
        """
        # Leave the input untouched if the 3D conv block is Identity
        if isinstance(block, nn.Identity):
            return x_3d, x_seen, mod_dict

        # Initialize index and indexation mode
        idx = None
        mode = 'pick'

        # Non-sparse forward and reindexing
        if isinstance(x_3d, torch.Tensor):
            # Forward pass on the block while keeping track of the
            # sampler indices
            block.sampler.last_idx = None
            idx_ref = torch.arange(x_3d.shape[0])
            x_3d = block(x_3d)
            idx_sample = block.sampler.last_idx
            if (idx_sample == idx_ref).all():
                idx = None
            else:
                idx = idx_sample
            mode = 'pick'

        # MinkowskiEngine forward and reindexing
        elif me is not None and isinstance(x_3d, me.SparseTensor):
            mode = 'merge'

            # Forward pass on the block while keeping track of the
            # stride levels
            stride_in = x_3d.tensor_stride[0]
            x_3d = block(x_3d)
            stride_out = x_3d.tensor_stride[0]

            if stride_in == stride_out:
                idx = None
            else:
                src, target = x_3d.coords_man.get_coords_map(
                    stride_in, stride_out)
                idx = target[src.argsort()]

        # TorchSparse forward and reindexing
        elif ts is not None and isinstance(x_3d, ts.SparseTensor):
            # Forward pass on the block while keeping track of the
            # stride levels
            stride_in = x_3d.s
            x_3d = block(x_3d)
            stride_out = x_3d.s

            if stride_in == stride_out:
                idx = None
            else:
                # To compute the reindexing of the sparse voxels with
                # torchsparse, we need to make use of the torchsparse
                # sphashquery function to compare sets of coordinates at
                # the same resolution. However, when changing resolution
                # we must be careful to voxelize spatial points but
                # leave the batch indices untouched. For torchsparse,
                # the batch indices are stored in the last column of
                # the coords tensor (unlike MinkowskiEngine which
                # stores batch indices in the first column). Hence we
                # assume here that coordinates to have shape (N x 4) and
                # batch indices to lie in the last column.
                assert x_3d.C.shape[1] == 4, \
                    f"Sparse coordinates are expected to have shape " \
                    f"(N x 4), with batch indices in the first column and " \
                    f"3D spatial coordinates in the following ones. Yet, " \
                    f"received coordinates tensor with shape {x_3d.C.shape} " \
                    f"instead."
                in_coords = x_3d.coord_maps[stride_in]
                in_coords[:, :3] = ((in_coords[:, :3].float() / stride_out
                                     ).floor() * stride_out).int()
                out_coords = x_3d.coord_maps[stride_out]
                idx = sphashquery(sphash(in_coords), sphash(out_coords))
            mode = 'merge'

        else:
            raise NotImplementedError(
                f"Unsupported format for x_3d: {type(x_3d)}. If you are trying "
                f"to use MinkowskiEngine or TorchSparse, make sure those are "
                f"properly installed.")

        # Update seen 3D points indices
        if x_seen is not None and idx is not None:
            if mode == 'pick':
                x_seen = x_seen[idx]
            else:
                x_seen = torch_scatter.scatter(x_seen, idx, reduce='sum')

        # Update modality data and mappings wrt new point indexing
        for m in mod_dict.keys():
            mod_dict[m] = mod_dict[m].select_points(idx, mode=mode)

        return x_3d, x_seen, mod_dict


class UnimodalBranch(nn.Module, ABC):
    """Unimodal block with downsampling that looks like:

    IN 3D    ------------------------------------           --   OUT 3D
                                    \            \         /
    IN Mod   -- Conv -- Atomic Pool -- View Pool -- Fusion
                      \
                       ---------------------------------------   OUT Mod

    The convolution may be a down-convolution or preserve input shape.
    However, up-convolutions are not supported, because reliable the
    mappings cannot be inferred when increasing resolution.
    """

    def __init__(self, conv, atomic_pool, view_pool, fusion, drop_3d=0,
            drop_mod=0):
        super(UnimodalBranch, self).__init__()
        self.conv = conv if conv is not None else Identity()
        self.atomic_pool = atomic_pool
        self.view_pool = view_pool
        self.fusion = fusion
        self.drop_3d = nn.Dropout(p=drop_3d) if drop_3d > 0 else nn.Identity()
        self.drop_mod = nn.Dropout(p=drop_mod) if drop_mod > 0 else nn.Identity()

    def forward(self, mm_data_tuple):
        # Unpack the multimodal data tuple
        x_3d, _, mod_data = mm_data_tuple

        # Check whether the modality carries multi-setting data
        has_multi_setting = isinstance(mod_data.x, list)

        # Conv on the modality data. The modality data holder
        # carries a feature tensor per modality settings. Hence the
        # modality features are provided as a list of tensors.
        # Update modality features and mappings wrt modality scale.
        # Note that convolved features are preserved in the modality
        # data holder, to be later used in potential downstream
        # modules.
        if has_multi_setting:
            for i in range(len(mod_data)):
                mod_data[i].update_features_and_scale(self.conv(mod_data[i].x))
        else:
            mod_data = mod_data.update_features_and_scale(
                self.conv(mod_data.x))

        # Extract CSR-arranged atomic features from the feature maps
        # of each input modality setting
        if has_multi_setting:
            x_mod = [x[idx]
                     for x, idx
                     in zip(mod_data.x, mod_data.feature_map_indexing)]
        else:
            x_mod = mod_data.x[mod_data.feature_map_indexing]

        # Atomic pooling of the modality features on each
        # separate setting
        if has_multi_setting:
            x_mod = [self.atomic_pool(x_3d, x, None, a_idx)[0]
                     for x, a_idx
                     in zip(x_mod, mod_data.atomic_csr_indexing)]
        else:
            x_mod = self.atomic_pool(x_3d, x_mod, None,
                                     mod_data.atomic_csr_indexing)[0]

        # For multi-setting data, concatenate view-level features from
        # each input modality setting and sort them to a CSR-friendly
        # order wrt 3D points features
        if has_multi_setting:
            idx_sorting = mod_data.view_cat_sorting
            x_mod = torch.cat(x_mod, dim=0)[idx_sorting]
            x_proj = torch.cat(mod_data.projection_features, dim=0)[idx_sorting]

        # View pooling of the atomic-pooled modality features
        if has_multi_setting:
            x_mod, x_seen = self.view_pool(
                x_3d, x_mod, x_proj, mod_data.view_cat_csr_indexing)
        else:
            x_mod, x_seen = self.view_pool(
                x_3d, x_mod, x_proj, mod_data.view_csr_indexing)

        # Dropout 3D or modality features
        if isinstance(x_3d, torch.Tensor):
            x_3d = self.drop_3d(x_3d)
        else:
            x_3d.F = self.drop_3d(x_3d.F)
        x_mod = self.drop_mod(x_mod)

        # drop = 0.3  # probability to apply dropout on either 3D or modality
        # drop_3d = 0.  # probability that 3D is the one dropped over modality
        #
        # idx_drop = torch.where(torch.rand(x_3d.shape[0], device=x_3d.device) < drop)[0]
        # subidx_drop = torch.rand(idx_drop.shape[0], device=x_3d.device) < drop_3d
        #
        # x_3d[idx_drop[subidx_drop]] *= 0
        # x_mod[idx_drop[~subidx_drop]] *= 0

        # Fuse the modality features into the 3D points features
        x_3d = self.fusion(x_3d, x_mod)

        return x_3d, x_seen, mod_data
