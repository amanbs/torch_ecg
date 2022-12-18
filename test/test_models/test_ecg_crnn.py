"""
"""

import itertools
from copy import deepcopy
from pathlib import Path

import pytest
import torch
from tqdm.auto import tqdm

from torch_ecg.models import ECG_CRNN
from torch_ecg.models.ecg_crnn import ECG_CRNN_v1
from torch_ecg.model_configs import ECG_CRNN_CONFIG


_TMP_DIR = Path(__file__).parents[1] / "tmp"
_TMP_DIR.mkdir(exist_ok=True)


@torch.no_grad()
def test_ecg_crnn():
    n_leads = 12
    classes = ["NSR", "AF", "PVC", "LBBB", "RBBB", "PAB", "VFL"]
    inp = torch.randn(2, n_leads, 5000)

    with pytest.warns(
        RuntimeWarning, match="No config is provided, using default config"
    ):
        model = ECG_CRNN(classes=classes, n_leads=n_leads)

    grid = itertools.product(
        [cnn_name for cnn_name in ECG_CRNN_CONFIG.cnn.keys() if cnn_name != "name"],
        [rnn_name for rnn_name in ECG_CRNN_CONFIG.rnn.keys() if rnn_name != "name"]
        + ["none"],
        [attn_name for attn_name in ECG_CRNN_CONFIG.attn.keys() if attn_name != "name"]
        + ["none"],
        ["none", "max", "avg"],  # global pool
    )
    total = (
        (len(ECG_CRNN_CONFIG.cnn.keys()) - 1)
        * len(ECG_CRNN_CONFIG.rnn.keys())
        * len(ECG_CRNN_CONFIG.attn.keys())
        * 3
    )

    for cnn_name, rnn_name, attn_name, global_pool in tqdm(grid, total=total):
        config = deepcopy(ECG_CRNN_CONFIG)
        config.cnn.name = cnn_name
        config.rnn.name = rnn_name
        config.attn.name = attn_name
        config.global_pool = global_pool
        model = ECG_CRNN(classes=classes, n_leads=n_leads, config=config)
        model = model.eval()
        out = model(inp)
        assert out.shape == model.compute_output_shape(
            seq_len=inp.shape[-1], batch_size=inp.shape[0]
        )

    doi = model.doi
    assert isinstance(doi, list)
    assert all([isinstance(d, str) for d in doi])

    with pytest.raises(
        NotImplementedError, match="implement a task specific inference method"
    ):
        model.inference(inp)

    with pytest.raises(NotImplementedError, match="CNN \042.+\042 not implemented yet"):
        config = deepcopy(ECG_CRNN_CONFIG)
        config.cnn.name = "not_implemented"
        config.cnn.not_implemented = {}
        ECG_CRNN(classes=classes, n_leads=n_leads, config=config)

    with pytest.raises(NotImplementedError, match="RNN \042.+\042 not implemented yet"):
        config = deepcopy(ECG_CRNN_CONFIG)
        config.rnn.name = "not_implemented"
        config.rnn.not_implemented = {}
        ECG_CRNN(classes=classes, n_leads=n_leads, config=config)

    with pytest.raises(
        NotImplementedError, match="Attention \042.+\042 not implemented yet"
    ):
        config = deepcopy(ECG_CRNN_CONFIG)
        config.attn.name = "not_implemented"
        config.attn.not_implemented = {}
        ECG_CRNN(classes=classes, n_leads=n_leads, config=config)

    with pytest.raises(
        NotImplementedError, match="Global Pooling \042.+\042 not implemented yet"
    ):
        config = deepcopy(ECG_CRNN_CONFIG)
        config.global_pool = "not_implemented"
        ECG_CRNN(classes=classes, n_leads=n_leads, config=config)

    with pytest.raises(
        NotImplementedError, match="Attentive pooling not implemented yet"
    ):
        config = deepcopy(ECG_CRNN_CONFIG)
        config.global_pool = "attn"
        ECG_CRNN(classes=classes, n_leads=n_leads, config=config)


def test_from_v1():
    config = deepcopy(ECG_CRNN_CONFIG)
    n_leads = 12
    classes = ["NSR", "AF", "PVC", "LBBB", "RBBB", "PAB", "VFL"]
    model_v1 = ECG_CRNN_v1(classes=classes, n_leads=n_leads, config=config)
    model_v1.save(
        _TMP_DIR / "ecg_crnn_v1.pth", {"classes": classes, "n_leads": n_leads}
    )
    model = ECG_CRNN.from_v1(_TMP_DIR / "ecg_crnn_v1.pth")
    (_TMP_DIR / "ecg_crnn_v1.pth").unlink()
    del model_v1, model
