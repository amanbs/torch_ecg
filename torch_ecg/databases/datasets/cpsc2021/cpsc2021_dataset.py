"""
data generator for feeding data into pytorch models

NOTE
----
In order to avoid potential error in the methods of slicing signals and rr intervals,
one can check using the following code

.. code-block:: python

    from torch_ecg.databases.datasets.cpsc2021 import CPSC2021Dataset, CPSC2021TrainCfg

    ds_train = CPSC2021Dataset(CPSC2021TrainCfg, task="qrs_detection", training=True)
    ds_val = CPSC2021Dataset(CPSC2021TrainCfg, task="qrs_detection", training=False)
    err_list = []
    for idx, seg in enumerate(ds_train.segments):
        sig, lb = ds_train[idx]
        if sig.shape != (2,6000) or lb.shape != (750, 1):
            print("\n"+f"segment {seg} has sig.shape = {sig.shape}, lb.shape = {lb.shape}"+"\n")
            err_list.append(seg)
        print(f"{idx+1}/{len(ds_train)}", end="\r")
    for idx, seg in enumerate(ds_val.segments):
        sig, lb = ds_val[idx]
        if sig.shape != (2,6000) or lb.shape != (750, 1):
            print("\n"+f"segment {seg} has sig.shape = {sig.shape}, lb.shape = {lb.shape}"+"\n")
            err_list.append(seg)
        print(f"{idx+1}/{len(ds_val)}", end="\r")
    for idx, seg in enumerate(err_list):
        path = ds_train._get_seg_data_path(seg)
        os.remove(path)
        path = ds_train._get_seg_ann_path(seg)
        os.remove(path)
        print(f"{idx+1}/{len(err_list)}", end="\r")

and similarly for the task of `rr_lstm`

"""

import json
import os
import re
import time
import warnings
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union, Any

import numpy as np
from scipy import signal as SS
from scipy.io import loadmat, savemat
from torch.utils.data.dataset import Dataset
from tqdm.auto import tqdm

from ...._preprocessors import PreprocManager
from ....databases import CPSC2021 as CR
from ....utils.misc import (
    ReprMixin,
    get_record_list_recursive3,
    list_sum,
    nildent,
    add_docstring,
)
from ....utils.utils_data import mask_to_intervals
from ....utils.utils_signal import remove_spikes_naive
from ....utils.utils_data import generate_weight_mask
from ....cfg import CFG, DEFAULTS
from .cpsc2021_cfg import CPSC2021TrainCfg


__all__ = [
    "CPSC2021Dataset",
]


class CPSC2021Dataset(ReprMixin, Dataset):
    """Data generator for feeding data into pytorch models
    using the :class:`~torch_ecg.databases.CPSC2021` database.

    Strategies for generating data and labels:
    1. ECGs are preprocessed and stored in one folder
    2. preprocessed ECGs are sliced with overlap to generate data and label for different tasks:

       - the data files stores segments of fixed length of preprocessed ECGs,
       - the annotation files contain "qrs_mask", and "af_mask"

    The returned values (tuple) of :meth:`__getitem__` depends on the task:

        1. "qrs_detection": (`data`, `qrs_mask`, None)
        2. "rr_lstm": (`rr_seq`, `rr_af_mask`, `rr_weight_mask`)
        3. "main": (`data`, `af_mask`, `weight_mask`)

    where

        - `data` shape: ``(n_lead, n_sample)``
        - `qrs_mask` shape: ``(n_sample, 1)``
        - `af_mask` shape: ``(n_sample, 1)``
        - `weight_mask` shape: ``(n_sample, 1)``
        - `rr_seq` shape: ``(n_rr, 1)``
        - `rr_af_mask` shape: ``(n_rr, 1)``
        - `rr_weight_mask` shape: ``(n_rr, 1)``

    Typical values of ``n_sample`` and ``n_rr`` are 6000 and 30, respectively.

    ``n_lead`` is typically 2, which is the number of leads
    in the ECG signal of the :class:`CPSC2021` database.

    Parameters
    ----------
    config : dict
        Configurations for the dataset, ref. `CPSC2021TrainCfg`.
        A simple example is as follows:

        .. code-block:: python

            >>> config = deepcopy(CPSC2021TrainCfg)
            >>> config.db_dir = "some/path/to/db"
            >>> dataset = CPSC2021Dataset(config, task="main", training=True, lazy=False)

    training : bool, default True
        If True, the training set will be loaded,
        otherwise the test (val) set will be loaded.
    lazy : bool, default True
        If True, the data will not be loaded immediately,
        instead, it will be loaded on demand.
    **reader_kwargs : dict, optional
        Keyword arguments for the database reader class.

    """

    __DEBUG__ = False
    __name__ = "CPSC2021Dataset"

    def __init__(
        self,
        config: CFG,
        task: str,
        training: bool = True,
        lazy: bool = True,
        **reader_kwargs: Any,
    ) -> None:
        super().__init__()
        self.config = deepcopy(config)
        if reader_kwargs.pop("db_dir", None) is not None:
            warnings.warn(
                "`db_dir` is specified in both config and reader_kwargs", RuntimeWarning
            )
        self.reader = CR(db_dir=self.config.db_dir, **reader_kwargs)
        # assert self.config.db_dir is not None, "db_dir must be specified"
        self.config.db_dir = self.reader.db_dir
        self.dtype = self.config.np_dtype
        self.allowed_preproc = list(
            set(
                [
                    "bandpass",
                    "baseline_remove",
                ]
            ).intersection(set(self.config.keys()))
        )

        self.training = training

        self.lazy = lazy

        ppm_config = CFG(random=False)
        ppm_config.update(deepcopy(self.config))
        ppm_config.pop("normalize")
        seg_ppm_config = CFG(random=False)
        seg_ppm_config.update(deepcopy(self.config))
        seg_ppm_config.pop("bandpass")
        self.ppm = PreprocManager.from_config(ppm_config)
        self.seg_ppm = PreprocManager.from_config(seg_ppm_config)

        # create directories if needed
        # preprocess_dir stores pre-processed signals
        self.preprocess_dir = self.config.db_dir / "preprocessed"
        self.preprocess_dir.mkdir(parents=True, exist_ok=True)
        # segments_dir for sliced segments of fixed length
        self.segments_base_dir = self.config.db_dir / "segments"
        self.segments_base_dir.mkdir(parents=True, exist_ok=True)
        self.segment_name_pattern = "S_[\\d]{1,3}_[\\d]{1,2}_[\\d]{7}"
        self.segment_ext = "mat"
        # rr_dir for sequence of rr intervals of fix length
        self.rr_seq_base_dir = self.config.db_dir / "rr_seq"
        self.rr_seq_base_dir.mkdir(parents=True, exist_ok=True)
        self.rr_seq_name_pattern = "R_[\\d]{1,3}_[\\d]{1,2}_[\\d]{7}"
        self.rr_seq_ext = "mat"

        self._all_data = None
        self._all_labels = None
        self._all_masks = None
        self.__set_task(task, lazy=self.lazy)

    def _load_all_data(self) -> None:
        """Load all data into memory."""
        self.__set_task(self.task, lazy=False)

    def __set_task(self, task: str, lazy: bool = True) -> None:
        """Set the task and load the data if needed.

        Parameters
        ----------
        task : str
            Name of the task, can be one of `CPSC2021TrainCfg.tasks`
        lazy : bool, default True
            If True, the data will not be loaded immediately,
            instead, it will be loaded on demand.

        Returns
        -------
        None

        """
        assert task.lower() in CPSC2021TrainCfg.tasks, f"illegal task \042{task}\042"
        if (
            hasattr(self, "task")
            and self.task == task.lower()
            and self._all_data is not None
            and len(self._all_data) > 0
        ):
            return
        self.task = task.lower()
        self.all_classes = self.config[task].classes
        self.n_classes = len(self.config[task].classes)
        self.lazy = lazy

        self.seglen = self.config[task].input_len  # alias, for simplicity
        split_res = self._train_test_split(
            train_ratio=self.config.train_ratio,
            force_recompute=False,
        )
        if self.training:
            self.subjects = split_res.train
        else:
            self.subjects = split_res.test

        if self.task in [
            "qrs_detection",
            "main",
        ]:
            # for qrs detection, or for the main task
            self.segments_dirs = CFG()
            self.__all_segments = CFG()
            self.segments_json = self.segments_base_dir / "segments.json"
            self._ls_segments()
            self.segments = list_sum(
                [self.__all_segments[subject] for subject in self.subjects]
            )
            if self.__DEBUG__:
                self.segments = DEFAULTS.RNG_sample(
                    self.segments, int(len(self.segments) * 0.01)
                ).tolist()
            if self.training:
                DEFAULTS.RNG.shuffle(self.segments)
            # preload data
            self.fdr = _FastDataReader(
                self.config,
                self.task,
                self.seg_ppm,
                self.segments_dirs,
                self.segments,
                self.segment_ext,
            )
            if self.lazy:
                return
            self._all_data, self._all_labels, self._all_masks = [], [], []
            with tqdm(
                range(len(self.fdr)),
                desc="Loading data",
                unit="record",
                dynamic_ncols=True,
                mininterval=1.0,
            ) as pbar:
                for idx in pbar:
                    d, l, m = self.fdr[idx]
                    self._all_data.append(d)
                    self._all_labels.append(l)
                    self._all_masks.append(m)
            self._all_data = np.array(self._all_data).astype(self.dtype)
            self._all_labels = np.array(self._all_labels).astype(self.dtype)
            if self.task == "qrs_detection":
                self._all_masks = None
            else:
                self._all_masks = np.array(self._all_masks).astype(self.dtype)
        elif self.task in [
            "rr_lstm",
        ]:
            self.rr_seq_dirs = CFG()
            self.__all_rr_seq = CFG()
            self.rr_seq_json = self.rr_seq_base_dir / "rr_seq.json"
            self._ls_rr_seq()
            self.rr_seq = list_sum(
                [self.__all_rr_seq[subject] for subject in self.subjects]
            )
            if self.__DEBUG__:
                self.rr_seq = DEFAULTS.RNG_sample(
                    self.rr_seq, int(len(self.rr_seq) * 0.01)
                ).tolist()
            if self.training:
                DEFAULTS.RNG.shuffle(self.rr_seq)
            # preload data
            self.fdr = _FastDataReader(
                self.config,
                self.task,
                self.seg_ppm,
                self.rr_seq_dirs,
                self.rr_seq,
                self.rr_seq_ext,
            )
            if self.lazy:
                return
            self._all_data, self._all_labels, self._all_masks = [], [], []
            with tqdm(
                range(len(self.fdr)),
                desc="Loading data",
                unit="record",
                dynamic_ncols=True,
                mininterval=1.0,
            ) as pbar:
                for idx in pbar:
                    d, l, m = self.fdr[idx]
                    self._all_data.append(d)
                    self._all_labels.append(l)
                    self._all_masks.append(m)
            self._all_data = np.array(self._all_data).astype(self.dtype)
            self._all_labels = np.array(self._all_labels).astype(self.dtype)
            self._all_masks = np.array(self._all_masks).astype(self.dtype)
        else:
            raise NotImplementedError(
                f"data generator for task \042{self.task}\042 not implemented"
            )

    def reset_task(self, task: str, lazy: bool = True) -> None:
        """Reset the task of the data generator.

        Parameters
        ----------
        task : str
            The task to be set.
        lazy : bool, optional
            If True, the data will not be loaded immediately,
            instead, it will be loaded on demand.

        Returns
        -------
        None

        """
        self.__set_task(task, lazy)

    def _ls_segments(self) -> None:
        """Find all segments in the segments directory,
        and store them in some private attributes.
        """
        for item in ["data", "ann"]:
            self.segments_dirs[item] = CFG()
            for s in self.reader.all_subjects:
                self.segments_dirs[item][s] = self.segments_base_dir / item / s
                self.segments_dirs[item][s].mkdir(parents=True, exist_ok=True)
        if self.segments_json.is_file():
            self.__all_segments = json.loads(self.segments_json.read_text())
            # return
        print(
            f"please allow the reader a few minutes to collect the segments from {self.segments_base_dir}..."
        )
        seg_filename_pattern = f"{self.segment_name_pattern}\\.{self.segment_ext}"
        self.__all_segments = CFG(
            {
                s: get_record_list_recursive3(
                    str(self.segments_dirs.data[s]), seg_filename_pattern
                )
                for s in self.reader.all_subjects
            }
        )
        if all([len(self.__all_segments[s]) > 0 for s in self.reader.all_subjects]):
            self.segments_json.write_text(
                json.dumps(self.__all_segments, ensure_ascii=False)
            )

    def _ls_rr_seq(self) -> None:
        """Find all rr sequences in the rr sequences directory,
        and store them in some private attributes.
        """
        for s in self.reader.all_subjects:
            self.rr_seq_dirs[s] = self.rr_seq_base_dir / s
            self.rr_seq_dirs[s].mkdir(parents=True, exist_ok=True)
        if self.rr_seq_json.is_file():
            self.__all_rr_seq = json.loads(self.rr_seq_json.read_text())
            # return
        print(
            f"please allow the reader a few minutes to collect the rr sequences from {self.rr_seq_base_dir}..."
        )
        rr_seq_filename_pattern = f"{self.rr_seq_name_pattern}\\.{self.rr_seq_ext}"
        self.__all_rr_seq = CFG(
            {
                s: get_record_list_recursive3(
                    self.rr_seq_dirs[s], rr_seq_filename_pattern
                )
                for s in self.reader.all_subjects
            }
        )
        if all([len(self.__all_rr_seq[s]) > 0 for s in self.reader.all_subjects]):
            self.rr_seq_json.write_text(
                json.dumps(self.__all_rr_seq, ensure_ascii=False)
            )

    @property
    def all_segments(self) -> CFG:
        if self.task in [
            "qrs_detection",
            "main",
        ]:
            return self.__all_segments
        else:
            return CFG()

    @property
    def all_rr_seq(self) -> CFG:
        if self.task.lower() in [
            "rr_lstm",
        ]:
            return self.__all_rr_seq
        else:
            return CFG()

    def __len__(self) -> int:
        return len(self.fdr)

    def __getitem__(self, index: int) -> Tuple[np.ndarray, ...]:
        if self.lazy:
            if self.task in ["qrs_detection"]:
                return self.fdr[index][:2]
            else:
                return self.fdr[index]
        else:
            if self.task in ["qrs_detection"]:
                return self._all_data[index], self._all_labels[index]
            else:
                return (
                    self._all_data[index],
                    self._all_labels[index],
                    self._all_masks[index],
                )

    def _get_seg_data_path(self, seg: str) -> Path:
        """Get the path of the data file of segment.

        Parameters
        ----------
        seg : str
            Name of the segment, of pattern like "S_1_1_0000193".

        Returns
        -------
        pathlib.Path
            Absolute path of the data file of the segment.

        """
        subject = seg.split("_")[1]
        fp = self.segments_dirs.data[subject] / f"{seg}.{self.segment_ext}"
        return fp

    @add_docstring(_get_seg_data_path.__doc__.replace("data file", "annotation file"))
    def _get_seg_ann_path(self, seg: str) -> Path:
        subject = seg.split("_")[1]
        fp = self.segments_dirs.ann[subject] / f"{seg}.{self.segment_ext}"
        return fp

    def _load_seg_data(self, seg: str) -> np.ndarray:
        """Load the data of the segment.

        Parameters
        ----------
        seg : str
            Name of the segment, of pattern like "S_1_1_0000193".

        Returns
        -------
        numpy.ndarray
            Loaded data of the segment, of shape ``(2, self.seglen)``.

        """
        seg_data_fp = self._get_seg_data_path(seg)
        seg_data = loadmat(str(seg_data_fp))["ecg"]
        return seg_data

    def _load_seg_ann(self, seg: str) -> dict:
        """Load the annotations of the segment.

        Parameters
        ----------
        seg : str
            Name of the segment, of pattern like "S_1_1_0000193".

        Returns
        -------
        seg_ann : dict
            Annotations of the segment, containing:

                - rpeaks: indices of rpeaks of the segment
                - qrs_mask: mask of qrs complexes of the segment
                - af_mask: mask of af episodes of the segment
                - interval: interval ([start_idx, end_idx]) in
                  the original ECG record of the segment

        """
        seg_ann_fp = self._get_seg_ann_path(seg)
        seg_ann = {
            k: v.flatten()
            for k, v in loadmat(str(seg_ann_fp)).items()
            if not k.startswith("__")
        }
        return seg_ann

    def _load_seg_mask(
        self, seg: str, task: Optional[str] = None
    ) -> Union[np.ndarray, Dict[str, np.ndarray]]:
        """Load the mask(s) of segment.

        Parameters
        ----------
        seg : str
            Name of the segment, of pattern like "S_1_1_0000193".
        task : str, optional
            Task of the mask(s) to be loaded, by default the current task.
            If is not None, the current task is overrided;
            if is "all", then all masks ("qrs_mask", "af_mask", etc.)
            will be returned.

        Returns
        -------
        numpy.ndarray or dict
            Mask(s) of the segment,
            of shape ``(self.seglen, self.n_classes)``.

        """
        seg_mask = {
            k: v.reshape((self.seglen, -1))
            for k, v in self._load_seg_ann(seg).items()
            if k
            in [
                "qrs_mask",
                "af_mask",
            ]
        }
        _task = (task or self.task).lower()
        if _task == "all":
            return seg_mask
        if _task in [
            "qrs_detection",
        ]:
            seg_mask = seg_mask["qrs_mask"]
        elif _task in [
            "main",
        ]:
            seg_mask = seg_mask["af_mask"]
        return seg_mask

    def _load_seg_seq_lab(self, seg: str, reduction: int) -> np.ndarray:
        """Load sequence labeling annotations of the segment.

        Parameters
        ----------
        seg : str
            Name of the segment, of pattern like "S_1_1_0000193".
        reduction : int
            Reduction ratio (granularity) of length of the model output,
            compared to the original signal length.

        Returns
        -------
        numpy.ndarray
            Label of the sequence,
            of shape ``(self.seglen//reduction, self.n_classes)``.

        """
        seg_mask = self._load_seg_mask(seg)
        seg_len, n_classes = seg_mask.shape
        seq_lab = np.stack(
            arrays=[
                np.mean(
                    seg_mask[reduction * idx : reduction * (idx + 1)],
                    axis=0,
                    keepdims=True,
                ).astype(int)
                for idx in range(seg_len // reduction)
            ],
            axis=0,
        ).squeeze(axis=1)
        return seq_lab

    def _get_rr_seq_path(self, seq_name: str) -> Path:
        """Get the path of the annotation file of the rr_seq.

        Parameters
        ----------
        seq_name : str
            Name of the rr_seq, of pattern like "R_1_1_0000193".

        Returns
        -------
        pathlib.Path
            absolute path of the annotation file of the rr_seq.

        """
        subject = seq_name.split("_")[1]
        fp = self.rr_seq_dirs[subject] / f"{seq_name}.{self.rr_seq_ext}"
        return fp

    def _load_rr_seq(self, seq_name: str) -> Dict[str, np.ndarray]:
        """Load the metadata of the rr_seq.

        Parameters
        ----------
        seq_name : str
            Name of the rr_seq, of pattern like "R_1_1_0000193".

        Returns
        -------
        dict
            metadata of sequence of rr intervals, including

                - rr: the sequence of rr intervals, with units in seconds,
                  of shape ``(self.seglen, 1)``
                - label: label of the rr intervals, 0 for normal, 1 for af,
                  of shape ``(self.seglen, self.n_classes)``
                - interval: interval of the current rr sequence in the whole
                  rr sequence in the original record

        """
        rr_seq_path = self._get_rr_seq_path(seq_name)
        rr_seq = {
            k: v for k, v in loadmat(str(rr_seq_path)).items() if not k.startswith("__")
        }
        rr_seq["rr"] = rr_seq["rr"].reshape((self.seglen, 1))
        rr_seq["label"] = rr_seq["label"].reshape((self.seglen, self.n_classes))
        rr_seq["interval"] = rr_seq["interval"].flatten()
        return rr_seq

    def persistence(self, force_recompute: bool = False, verbose: int = 0) -> None:
        """Save the preprocessed data to disk.

        Parameters
        ----------
        force_recompute : bool, default False
            Whether to force recompute the preprocessed data.
        verbose : int, default 0
            Verbosity level for printing the progress.

        Returns
        -------
        None

        """
        if verbose >= 1:
            print(" preprocessing data ".center(110, "#"))
        self._preprocess_data(
            force_recompute=force_recompute,
            verbose=verbose,
        )

        original_task = self.task
        original_lazy = self.lazy
        self.__set_task("main", lazy=True)
        if verbose >= 1:
            print("\n" + " slicing data into segments ".center(110, "#"))
        self._slice_data(
            force_recompute=force_recompute,
            verbose=verbose,
        )

        self.__set_task("rr_lstm", lazy=True)
        if verbose >= 1:
            print("\n" + " generating rr sequences ".center(110, "#"))
        self._slice_rr_seq(
            force_recompute=force_recompute,
            verbose=verbose,
        )

        self.__set_task(original_task, lazy=original_lazy)

    def _preprocess_data(self, force_recompute: bool = False, verbose: int = 0) -> None:
        """Preprocesses the ECG data in advance for further use.

        Parameters
        ----------
        force_recompute : bool, default False
            Whether to force recompute the preprocessed data.
        verbose : int, default 0
            Verbosity level for printing the progress.

        Returns
        -------
        None

        """
        for idx, rec in enumerate(self.reader.all_records):
            self._preprocess_one_record(
                rec=rec,
                force_recompute=force_recompute,
                verbose=verbose,
            )
            if verbose >= 1:
                print(f"{idx+1}/{len(self.reader.all_records)} records", end="\r")

    def _preprocess_one_record(
        self, rec: str, force_recompute: bool = False, verbose: int = 0
    ) -> None:
        """Preprocesses one ECG record in advance for further use.

        Parameters
        ----------
        rec : str
            Name of the record.
        force_recompute : bool, default False
            Whether to force recompute the preprocessed data.
        verbose : int, default 0
            Verbosity level for printing the progress.

        Returns
        -------
        None

        """
        suffix = self._get_rec_suffix(self.allowed_preproc)
        save_fp = self.preprocess_dir / f"{rec}-{suffix}.{self.segment_ext}"
        if (not force_recompute) and save_fp.is_file():
            return
        # perform pre-process
        pps, _ = self.ppm(self.reader.load_data(rec), self.config.fs)
        savemat(save_fp, {"ecg": pps}, format="5")

    def load_preprocessed_data(self, rec: str) -> np.ndarray:
        """Load the preprocessed data of the record.

        Parameters
        ----------
        rec : str
            Name of the record.

        Returns
        -------
        numpy.ndarray
            The pre-computed preprocessed ECG data of the record.

        """
        preproc = self.allowed_preproc
        suffix = self._get_rec_suffix(preproc)
        fp = self.preprocess_dir / f"{rec}-{suffix}.{self.segment_ext}"
        if not fp.is_file():
            raise FileNotFoundError(
                f"preprocess(es) \042{preproc}\042 not done for {rec} yet"
            )
        p_sig = loadmat(str(fp))["ecg"]
        if p_sig.shape[0] != 2:
            p_sig = p_sig.T
        return p_sig

    def _get_rec_suffix(self, operations: List[str]) -> str:
        """Get the suffix of the filename of the preprocessed ECG signal.

        Parameters
        ----------
        operations : List[str]
            Names of operations to perform (or has performed).
            Should be sublist of `self.allowed_preproc`.

        Returns
        -------
        str
            Suffix of the filename of the preprocessed ECG signal.

        """
        suffix = "-".join(sorted([item.lower() for item in operations]))
        return suffix

    def _slice_data(self, force_recompute: bool = False, verbose: int = 0) -> None:
        """Slice the preprocessed data into segments.

        Slice all records into segments of length `self.seglen`,
        and perform data augmentations specified in `self.config`.

        Parameters
        ----------
        force_recompute : bool, default False
            Whether to force recompute the preprocessed data.
        verbose : int, default 0
            Verbosity level for printing the progress.

        Returns
        -------
        None

        """
        self.__assert_task(
            [
                "qrs_detection",
                "main",
            ]
        )
        if force_recompute:
            self._clear_cached_segments()
        for idx, rec in enumerate(self.reader.all_records):
            self._slice_one_record(
                rec=rec,
                force_recompute=False,
                update_segments_json=False,
                verbose=verbose,
            )
            if verbose >= 1:
                print(f"{idx+1}/{len(self.reader.all_records)} records", end="\r")
        if force_recompute:
            self.segments_json.write_text(
                json.dump(self.__all_segments, ensure_ascii=False)
            )

    def _slice_one_record(
        self,
        rec: str,
        force_recompute: bool = False,
        update_segments_json: bool = False,
        verbose: int = 0,
    ) -> None:
        """Slice one preprocessed record into segments.

        slice one record into segments of length `self.seglen`,
        and perform data augmentations specified in `self.config`.

        Parameters
        ----------
        rec : str
            Name of the record.
        force_recompute : bool, default False
            Whether to force recompute the preprocessed data.
        update_segments_json : bool, default False
            If both `force_recompute` and `update_segments_json` are True,
            the file `self.segments_json` will be updated.
            Useful when slicing not all records.
        verbose : int, default 0
            Verbosity level for printing the progress.

        Returns
        -------
        None

        """
        self.__assert_task(
            [
                "qrs_detection",
                "main",
            ]
        )
        subject = self.reader.get_subject_id(rec)
        rec_segs = [
            item
            for item in self.__all_segments[subject]
            if item.startswith(rec.replace("data", "S"))
        ]
        if (not force_recompute) and len(rec_segs) > 0:
            return
        elif force_recompute:
            self._clear_cached_segments([rec])

        # data = self.reader.load_data(rec, units="mV")
        data = self.load_preprocessed_data(rec)
        siglen = data.shape[1]
        rpeaks = self.reader.load_rpeaks(rec)
        af_mask = self.reader.load_af_episodes(rec, fmt="mask")
        forward_len = self.seglen - self.config[self.task].overlap_len
        critical_forward_len = self.seglen - self.config[self.task].critical_overlap_len
        critical_forward_len = [critical_forward_len // 4, critical_forward_len]

        # skip those records that are too short
        if siglen < self.seglen:
            return

        # find critical points
        critical_points = np.where(np.diff(af_mask) != 0)[0]
        critical_points = [
            p
            for p in critical_points
            if critical_forward_len[1] <= p < siglen - critical_forward_len[1]
        ]

        segments = []

        # ordinary segments with constant forward_len
        for idx in range((siglen - self.seglen) // forward_len + 1):
            start_idx = idx * forward_len
            new_seg = self.__generate_segment(
                rec=rec,
                data=data,
                start_idx=start_idx,
            )
            segments.append(new_seg)
        # the tail segment
        new_seg = self.__generate_segment(
            rec=rec,
            data=data,
            end_idx=siglen,
        )
        segments.append(new_seg)

        # special segments around critical_points with random forward_len in critical_forward_len
        for cp in critical_points:
            start_idx = max(
                0,
                cp
                - self.seglen
                + DEFAULTS.RNG_randint(
                    critical_forward_len[0], critical_forward_len[1]
                ),
            )
            while start_idx <= min(cp - critical_forward_len[1], siglen - self.seglen):
                new_seg = self.__generate_segment(
                    rec=rec,
                    data=data,
                    start_idx=start_idx,
                )
                segments.append(new_seg)
                start_idx += DEFAULTS.RNG_randint(
                    critical_forward_len[0], critical_forward_len[1]
                )

        # return segments
        self.__save_segments(rec, segments, update_segments_json)

    def __generate_segment(
        self,
        rec: str,
        data: np.ndarray,
        start_idx: Optional[int] = None,
        end_idx: Optional[int] = None,
    ) -> CFG:
        """Generate segment, with possible data augmentation.

        Parameter
        ---------
        rec : str
            Name of the record.
        data : numpy.ndarray
            The whole of (preprocessed) ECG record.
        start_idx : int, optional
            Start index of the signal for generating the segment.
        end_idx : int, optional
            End index of the signal for generating the segment.
            If `start_idx` is set, then `end_idx` is ignored,
            since the segment length is fixed to `self.seglen`.
            At least one of `start_idx` and `end_idx` should be set.

        Returns
        -------
        dict
            Segments (meta-)data, containing:

                - data: values of the segment, with units in mV
                - rpeaks: indices of rpeaks of the segment
                - qrs_mask: mask of qrs complexes of the segment
                - af_mask: mask of af episodes of the segment
                - interval: interval ([start_idx, end_idx]) in the
                  original ECG record of the segment

        """
        assert not all(
            [start_idx is None, end_idx is None]
        ), "at least one of `start_idx` and `end_idx` should be set"
        siglen = data.shape[1]
        # offline augmentations are done, including strech-or-compress, ...
        if self.config.stretch_compress != 0:
            stretch_compress_choices = [0, 1, -1]
            sign = DEFAULTS.RNG_sample(stretch_compress_choices, 1)[0]
            if sign != 0:
                sc_ratio = self.config.stretch_compress
                sc_ratio = (
                    1 + (DEFAULTS.RNG.uniform(sc_ratio / 4, sc_ratio) * sign) / 100
                )
                sc_len = int(round(sc_ratio * self.seglen))
                if start_idx is not None:
                    end_idx = start_idx + sc_len
                else:
                    start_idx = end_idx - sc_len
                if end_idx > siglen:
                    end_idx = siglen
                    start_idx = max(0, end_idx - sc_len)
                    sc_ratio = (end_idx - start_idx) / self.seglen
                aug_seg = data[..., start_idx:end_idx]
                aug_seg = SS.resample(x=aug_seg, num=self.seglen, axis=1)
            else:
                if start_idx is not None:
                    end_idx = start_idx + self.seglen
                    if end_idx > siglen:
                        end_idx = siglen
                        start_idx = end_idx - self.seglen
                else:
                    start_idx = end_idx - self.seglen
                    if start_idx < 0:
                        start_idx = 0
                        end_idx = self.seglen
                # the segment of original signal, with no augmentation
                aug_seg = data[..., start_idx:end_idx]
                sc_ratio = 1
        else:
            if start_idx is not None:
                end_idx = start_idx + self.seglen
                if end_idx > siglen:
                    end_idx = siglen
                    start_idx = end_idx - self.seglen
            else:
                start_idx = end_idx - self.seglen
                if start_idx < 0:
                    start_idx = 0
                    end_idx = self.seglen
            aug_seg = data[..., start_idx:end_idx]
            sc_ratio = 1
        # adjust rpeaks
        seg_rpeaks = self.reader.load_rpeaks(
            rec=rec,
            sampfrom=start_idx,
            sampto=end_idx,
            keep_original=False,
        )
        seg_rpeaks = [
            int(round(r / sc_ratio))
            for r in seg_rpeaks
            if self.config.rpeaks_dist2border
            <= r
            < self.seglen - self.config.rpeaks_dist2border
        ]
        # generate qrs_mask from rpeaks
        seg_qrs_mask = np.zeros((self.seglen,), dtype=int)
        for r in seg_rpeaks:
            seg_qrs_mask[
                r - self.config.qrs_mask_bias : r + self.config.qrs_mask_bias
            ] = 1
        # adjust af_intervals
        seg_af_intervals = self.reader.load_af_episodes(
            rec=rec,
            sampfrom=start_idx,
            sampto=end_idx,
            keep_original=False,
            fmt="intervals",
        )
        seg_af_intervals = [
            [int(round(itv[0] / sc_ratio)), int(round(itv[1] / sc_ratio))]
            for itv in seg_af_intervals
        ]
        # generate af_mask from af_intervals
        seg_af_mask = np.zeros((self.seglen,), dtype=int)
        for itv in seg_af_intervals:
            seg_af_mask[itv[0] : itv[1]] = 1

        new_seg = CFG(
            data=aug_seg,
            rpeaks=seg_rpeaks,
            qrs_mask=seg_qrs_mask,
            af_mask=seg_af_mask,
            interval=[start_idx, end_idx],
        )
        return new_seg

    def __save_segments(
        self, rec: str, segments: List[CFG], update_segments_json: bool = False
    ) -> None:
        """Save the segments to the disk.

        Parameters
        ----------
        rec : str
            Name of the record.
        segments : List[dict]
            List of the segments (meta-)data to be saved.
        update_segments_json : bool, default False
            Whether to update the segments.json file.

        Returns
        -------
        None

        """
        subject = self.reader.get_subject_id(rec)
        ordering = list(range(len(segments)))
        DEFAULTS.RNG.shuffle(ordering)
        for i, idx in enumerate(ordering):
            seg = segments[idx]
            filename = f"{rec}_{i:07d}.{self.segment_ext}".replace("data", "S")
            data_path = self.segments_dirs.data[subject] / filename
            savemat(str(data_path), {"ecg": seg.data})
            self.__all_segments[subject].append(Path(filename).with_suffix("").name)
            ann_path = self.segments_dirs.ann[subject] / filename
            savemat(
                str(ann_path),
                {
                    k: v
                    for k, v in seg.items()
                    if k
                    not in [
                        "data",
                    ]
                },
            )
        if update_segments_json:
            self.segments_json.write_text(
                json.dumps(self.__all_segments, ensure_ascii=False)
            )

    def _clear_cached_segments(self, recs: Optional[Sequence[str]] = None) -> None:
        """Clear the cached segments.

        Parameters
        ----------
        recs : Sequence[str], optional
            Sequence of the records whose segments are to be cleared.
            Defaults to all records.

        Returns
        -------
        None

        """
        self.__assert_task(
            [
                "qrs_detection",
                "main",
            ]
        )
        if recs is not None:
            for rec in recs:
                subject = self.reader.get_subject_id(rec)
                for item in [
                    "data",
                    "ann",
                ]:
                    path = str(self.segments_dirs[item][subject])
                    for f in [
                        n for n in os.listdir(path) if n.endswith(self.segment_ext)
                    ]:
                        if self._get_rec_name(f) == rec:
                            os.remove(os.path.join(path, f))
                            if os.path.splitext(f)[0] in self.__all_segments[subject]:
                                self.__all_segments[subject].remove(
                                    os.path.splitext(f)[0]
                                )
        else:
            for subject in self.reader.all_subjects:
                for item in [
                    "data",
                    "ann",
                ]:
                    path = str(self.segments_dirs[item][subject])
                    for f in [
                        n for n in os.listdir(path) if n.endswith(self.segment_ext)
                    ]:
                        os.remove(os.path.join(path, f))
                        if os.path.splitext(f)[0] in self.__all_segments[subject]:
                            self.__all_segments[subject].remove(os.path.splitext(f)[0])
        self.segments = list_sum(
            [self.__all_segments[subject] for subject in self.subjects]
        )

    def _slice_rr_seq(self, force_recompute: bool = False, verbose: int = 0) -> None:
        """Slice sequences of rr intervals into fixed length (sub)sequences.

        Parameters
        ----------
        force_recompute : bool, default False
            Whether to force recompute the rr sequences.
        verbose : int, default 0
            Verbosity level for printing the progress.

        Returns
        -------
        None

        """
        self.__assert_task(["rr_lstm"])
        if force_recompute:
            self._clear_cached_rr_seq()
        for idx, rec in enumerate(self.reader.all_records):
            self._slice_rr_seq_one_record(
                rec=rec,
                force_recompute=False,
                update_rr_seq_json=False,
                verbose=verbose,
            )
            if verbose >= 1:
                print(f"{idx+1}/{len(self.reader.all_records)} records", end="\r")
        if force_recompute:
            self.rr_seq_json.write_text(
                json.dumps(self.__all_rr_seq, ensure_ascii=False)
            )

    def _slice_rr_seq_one_record(
        self,
        rec: str,
        force_recompute: bool = False,
        update_rr_seq_json: bool = False,
        verbose: int = 0,
    ) -> None:
        """Slice sequences of rr intervals from one record
        into fixed length (sub)sequences.

        Parameters
        ----------
        rec : str
            Name of the record.
        force_recompute : bool, default False
            Whether to force recompute the rr sequences.
        update_rr_seq_json : bool, default False
            Whether to update the rr_seq.json file.
        verbose : int, default 0
            Verbosity level for printing the progress.

        Returns
        -------
        None

        """
        self.__assert_task(["rr_lstm"])
        subject = self.reader.get_subject_id(rec)
        rec_rr_seq = [
            item
            for item in self.__all_rr_seq[subject]
            if item.startswith(rec.replace("data", "R"))
        ]
        if (not force_recompute) and len(rec_rr_seq) > 0:
            return
        elif force_recompute:
            self._clear_cached_rr_seq([rec])

        forward_len = self.seglen - self.config[self.task].overlap_len
        critical_forward_len = self.seglen - self.config[self.task].critical_overlap_len
        critical_forward_len = [critical_forward_len - 2, critical_forward_len]

        rpeaks = self.reader.load_rpeaks(rec)
        rr = np.diff(rpeaks) / self.config.fs
        if len(rr) < self.seglen:
            return
        af_mask = self.reader.load_af_episodes(rec, fmt="mask")
        label_seq = af_mask[rpeaks][:-1]

        # find critical points
        critical_points = np.where(np.diff(label_seq) != 0)[0]
        critical_points = [
            p
            for p in critical_points
            if critical_forward_len[1] <= p < len(rr) - critical_forward_len[1]
        ]

        rr_seq = []

        # ordinary segments with constant forward_len
        for idx in range((len(rr) - self.seglen) // forward_len + 1):
            start_idx = idx * forward_len
            end_idx = start_idx + self.seglen
            new_rr_seq = CFG(
                rr=rr[start_idx:end_idx],
                label=label_seq[start_idx:end_idx],
                interval=[start_idx, end_idx],
            )
            rr_seq.append(new_rr_seq)
        # the tail segment
        if end_idx < len(rr):
            end_idx = len(rr)
            start_idx = end_idx - self.seglen
            new_rr_seq = CFG(
                rr=rr[start_idx:end_idx],
                label=label_seq[start_idx:end_idx],
                interval=[start_idx, end_idx],
            )
            rr_seq.append(new_rr_seq)

        # special segments around critical_points with random forward_len in critical_forward_len
        for cp in critical_points:
            start_idx = max(
                0,
                cp
                - self.seglen
                + DEFAULTS.RNG_randint(
                    critical_forward_len[0], critical_forward_len[1]
                ),
            )
            while start_idx <= min(cp - critical_forward_len[1], len(rr) - self.seglen):
                end_idx = start_idx + self.seglen
                new_rr_seq = CFG(
                    rr=rr[start_idx:end_idx],
                    label=label_seq[start_idx:end_idx],
                    interval=[start_idx, end_idx],
                )
                rr_seq.append(new_rr_seq)
                start_idx += DEFAULTS.RNG_randint(
                    critical_forward_len[0], critical_forward_len[1]
                )
        # save rr sequences
        self.__save_rr_seq(rec, rr_seq, update_rr_seq_json)

    def __save_rr_seq(
        self, rec: str, rr_seq: List[CFG], update_rr_seq_json: bool = False
    ) -> None:
        """Save the sliced rr sequences to disk.

        Parameters
        ----------
        rec : str,
            Name of the record.
        rr_seq : List[dict],
            List of the rr_seq (meta-)data.
        update_rr_seq_json : bool, default False
            Whether to update the rr_seq.json file.

        Returns
        -------
        None

        """
        subject = self.reader.get_subject_id(rec)
        ordering = list(range(len(rr_seq)))
        DEFAULTS.RNG.shuffle(ordering)
        for i, idx in enumerate(ordering):
            item = rr_seq[idx]
            filename = f"{rec}_{i:07d}.{self.rr_seq_ext}".replace("data", "R")
            data_path = self.rr_seq_dirs[subject] / filename
            savemat(str(data_path), item)
            self.__all_rr_seq[subject].append(Path(filename).with_suffix("").name)
        if update_rr_seq_json:
            self.rr_seq_json.write_text(
                json.dumps(self.__all_rr_seq, ensure_ascii=False)
            )

    def _clear_cached_rr_seq(self, recs: Optional[Sequence[str]] = None) -> None:
        """Clear the cached rr sequences.

        Parameters
        ----------
        recs : Sequence[str], optional
            Sequence of the records whose cached rr sequences are to be cleared.
            Defaults to all records.

        Returns
        -------
        None

        """
        self.__assert_task(["rr_lstm"])
        if recs is not None:
            for rec in recs:
                subject = self.reader.get_subject_id(rec)
                path = str(self.rr_seq_dirs[subject])
                for f in [n for n in os.listdir(path) if n.endswith(self.rr_seq_ext)]:
                    if self._get_rec_name(f) == rec:
                        os.remove(os.path.join(path, f))
                        if os.path.splitext(f)[0] in self.__all_rr_seq[subject]:
                            self.__all_rr_seq[subject].remove(os.path.splitext(f)[0])
        else:
            for subject in self.reader.all_subjects:
                path = str(self.rr_seq_dirs[subject])
                for f in [n for n in os.listdir(path) if n.endswith(self.rr_seq_ext)]:
                    os.remove(os.path.join(path, f))
                    if os.path.splitext(f)[0] in self.__all_rr_seq[subject]:
                        self.__all_rr_seq[subject].remove(os.path.splitext(f)[0])
        self.rr_seq = list_sum(
            [self.__all_rr_seq[subject] for subject in self.subjects]
        )

    def _get_rec_name(self, seg_or_rr: str) -> str:
        """Get the name of the record that a segment
        or rr_seq was generated from.

        Parameters
        ----------
        seg_or_rr : str
            Name of the segment or rr_seq.

        Returns
        -------
        rec : str
            Name of the record
            that the segment or rr_seq was generated from.

        """
        rec = re.sub("[RS]", "data", os.path.splitext(seg_or_rr)[0])[:-8]
        return rec

    def _train_test_split(
        self, train_ratio: float = 0.8, force_recompute: bool = False
    ) -> Dict[str, List[str]]:
        """Perform train-test split.

        Parameters
        ----------
        train_ratio : float, default 0.8
            Ratio of the train set in the whole dataset.
        force_recompute : bool, default False
            If True, the train-test split will be recomputed,
            regardless of the existing ones stored in json files.

        Returns
        -------
        dict
            Dictionary of the train-test split.
            Keys are "train" and "test", and
            values are list of the subjects split for training or validation.

        """
        start = time.time()
        print("\nstart performing train test split...\n")
        _train_ratio = int(train_ratio * 100)
        _test_ratio = 100 - _train_ratio
        assert _train_ratio * _test_ratio > 0

        train_file = self.reader.db_dir_base / f"train_ratio_{_train_ratio}.json"
        test_file = self.reader.db_dir_base / f"test_ratio_{_test_ratio}.json"

        if force_recompute or not all([train_file.is_file(), test_file.is_file()]):
            all_subjects = set(self.reader.df_stats.subject_id.tolist())
            afp_subjects = set(
                self.reader.df_stats[
                    self.reader.df_stats.label == "AFp"
                ].subject_id.tolist()
            )
            aff_subjects = (
                set(
                    self.reader.df_stats[
                        self.reader.df_stats.label == "AFf"
                    ].subject_id.tolist()
                )
                - afp_subjects
            )
            normal_subjects = all_subjects - afp_subjects - aff_subjects

            test_set = (
                DEFAULTS.RNG_sample(
                    list(afp_subjects),
                    max(1, int(round(len(afp_subjects) * _test_ratio / 100))),
                ).tolist()
                + DEFAULTS.RNG_sample(
                    list(aff_subjects),
                    max(1, int(round(len(aff_subjects) * _test_ratio / 100))),
                ).tolist()
                + DEFAULTS.RNG_sample(
                    list(normal_subjects),
                    max(1, int(round(len(normal_subjects) * _test_ratio / 100))),
                ).tolist()
            )
            train_set = list(all_subjects - set(test_set))

            DEFAULTS.RNG.shuffle(test_set)
            DEFAULTS.RNG.shuffle(train_set)

            train_file.write_text(json.dumps(train_set, ensure_ascii=False))
            test_file.write_text(json.dumps(test_set, ensure_ascii=False))
            print(
                nildent(
                    f"""
                train set saved to \n\042{str(train_file)}\042
                test set saved to \n\042{str(test_file)}\042
                """
                )
            )
        else:
            train_set = json.loads(train_file.read_text())
            test_set = json.loads(test_file.read_text())

        print(f"train test split finished in {(time.time()-start)/60:.2f} minutes")

        split_res = CFG(
            {
                "train": train_set,
                "test": test_set,
            }
        )
        return split_res

    def __assert_task(self, tasks: List[str]) -> None:
        """Check if the current task is in the given list of tasks."""
        assert self.task in tasks, (
            f"DO NOT call this method when the current task is {self.task}. "
            "Switch task using `reset_task`"
        )

    def plot_seg(self, seg: str, ticks_granularity: int = 0) -> None:
        """Plot the segment.

        Parameters
        ----------
        seg : str
            Name of the segment, of pattern like "S_1_1_0000193".
        ticks_granularity : int, default 0
            Granularity to plot axis ticks, the higher the more ticks.
            0 (no ticks) --> 1 (major ticks) --> 2 (major + minor ticks)

        Returns
        -------
        None

        """
        seg_data = self._load_seg_data(seg)
        print(f"seg_data.shape = {seg_data.shape}")
        seg_ann = self._load_seg_ann(seg)
        seg_ann["af_episodes"] = mask_to_intervals(seg_ann["af_mask"], vals=1)
        print(f"seg_ann = {seg_ann}")
        rec_name = self._get_rec_name(seg)
        self.reader.plot(
            rec=rec_name,  # unnecessary indeed
            data=seg_data,
            ann=seg_ann,
            ticks_granularity=ticks_granularity,
        )

    def extra_repr_keys(self) -> List[str]:
        return [
            "training",
            "task",
            "reader",
        ]


class _FastDataReader(ReprMixin, Dataset):
    """Fast data reader.

    Parameters
    ----------
    reader : CR
        The reader to read the data.
    records : Sequence[str]
        The list of records to read.
    config : CFG
        The configuration.
    ppm : PreprocManager, optional
        The preprocessor manager.

    """

    def __init__(
        self,
        config: CFG,
        task: str,
        seg_ppm: PreprocManager,
        file_dirs: dict,
        files: List[str],
        file_ext: str,
    ) -> None:
        self.config = config
        self.task = task
        self.seg_ppm = seg_ppm
        self.file_dirs = file_dirs
        self.files = files
        self.file_ext = file_ext

        self.seglen = self.config[self.task].input_len
        self.n_classes = len(self.config[task].classes)

        self._seg_keys = {
            "qrs_detection": "qrs_mask",
            "main": "af_mask",
        }

    def __getitem__(self, index: int) -> Tuple[np.ndarray, ...]:
        if self.task in [
            "qrs_detection",
            "main",
        ]:
            seg_name = self.files[index]
            subject = seg_name.split("_")[1]
            seg_data_fp = self.file_dirs.data[subject] / f"{seg_name}.{self.file_ext}"
            seg_data = loadmat(str(seg_data_fp))["ecg"]
            for idx in range(seg_data.shape[0]):
                seg_data[idx] = remove_spikes_naive(seg_data[idx])
            seg_ann_fp = self.file_dirs.ann[subject] / f"{seg_name}.{self.file_ext}"
            seg_label = loadmat(str(seg_ann_fp))[self._seg_keys[self.task]].reshape(
                (self.seglen, -1)
            )
            if self.config[self.task].reduction > 1:
                reduction = self.config[self.task].reduction
                seg_len, n_classes = seg_label.shape
                seg_label = np.stack(
                    arrays=[
                        np.mean(
                            seg_data[reduction * idx : reduction * (idx + 1)],
                            axis=0,
                            keepdims=True,
                        ).astype(int)
                        for idx in range(seg_len // reduction)
                    ],
                    axis=0,
                ).squeeze(axis=1)
            seg_data, _ = self.seg_ppm(seg_data, self.config.fs)
            if self.task == "main":
                weight_mask = generate_weight_mask(
                    target_mask=seg_label.squeeze(-1),
                    fg_weight=2,
                    fs=self.config.fs,
                    reduction=self.config[self.task].reduction,
                    radius=0.8,
                    boundary_weight=5,
                )[..., np.newaxis]
                return seg_data, seg_label, weight_mask
            return seg_data, seg_label, None
        elif self.task in [
            "rr_lstm",
        ]:
            seq_name = self.files[index]
            subject = seq_name.split("_")[1]
            rr_seq_path = self.file_dirs[subject] / f"{seq_name}.{self.file_ext}"
            rr_seq = loadmat(str(rr_seq_path))
            rr_seq["rr"] = rr_seq["rr"].reshape((self.seglen, 1))
            rr_seq["label"] = rr_seq["label"].reshape((self.seglen, self.n_classes))
            weight_mask = generate_weight_mask(
                target_mask=rr_seq["label"].squeeze(-1),
                fg_weight=2,
                fs=1 / 0.8,
                reduction=1,
                radius=2,
                boundary_weight=5,
            )[..., np.newaxis]
            return rr_seq["rr"], rr_seq["label"], weight_mask
        else:
            raise NotImplementedError(
                f"data generator for task \042{self.task}\042 not implemented"
            )

    def __len__(self) -> int:
        return len(self.files)

    def extra_repr_keys(self) -> List[str]:
        return [
            "task",
            "reader",
            "ppm",
        ]


# `StandaloneSegmentSlicer` can be found in `benchmarks/train_hybrid_cpsc2021/dataset.py`
