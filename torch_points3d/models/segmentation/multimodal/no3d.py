import logging
from abc import ABC

import torch.nn.functional as F
import torch.nn as nn

from torch_points3d.models.base_model import BaseModel
from torch_points3d.datasets.segmentation import IGNORE_LABEL
from torch_points3d.applications.multimodal.no3d import No3DEncoder

log = logging.getLogger(__name__)


class No3DSeg(BaseModel, ABC):
    def __init__(self, option, model_type, dataset, modules):
        # BaseModel init
        super().__init__(option)

        # UnwrappedUnetBasedModel init
        self.backbone = No3DEncoder(option, model_type, dataset, modules)

        # Segmentation head init
        self.head = nn.Sequential(nn.Linear(self.backbone.output_nc,
                                            dataset.num_classes))
        self.loss_names = ["loss_seg"]

    def set_input(self, data, device):
        self.batch_idx = data.batch.squeeze()
        self.input = data
        if data.y is not None:
            self.labels = data.y.to(self.device)
        else:
            self.labels = None

    def forward(self, *args, **kwargs):
        features = self.backbone(self.input).x
        logits = self.head(features)
        self.output = F.log_softmax(logits, dim=-1)
        if self.labels is not None:
            self.loss_seg = F.nll_loss(self.output, self.labels,
                                       ignore_index=IGNORE_LABEL)

    def backward(self):
        self.loss_seg.backward()
