import pickle
from addict import Dict
import os
import glob
from typing import Union
import zipfile
import io

import numpy as np
import torch
from torch.utils.data import Dataset, ConcatDataset
from torch.utils.data.dataloader import DataLoader
from torch.nn.utils.rnn import pad_sequence


class InvalidSetSize(Exception):
    pass


class ContactDataset(Dataset):
    SET_VALUED_FIELDS = [
        "encounter_health",
        "encounter_message",
        "encounter_partner_id",
        "encounter_day",
        "encounter_duration",
        "encounter_is_contagion",
    ]

    SEQUENCE_VALUED_FIELDS = [
        "health_history",
        "infectiousness_history",
        "history_days",
        "valid_history_mask",
    ]

    INPUT_FIELD_TO_SLICE_MAPPING = {
        "human_idx": ("human_idx", slice(None)),
        "day_idx": ("day_idx", slice(None)),
        "health_history": ("health_history", slice(None)),
        "reported_symptoms": ("health_history", slice(0, 12)),
        "test_results": ("health_history", slice(12, 13)),
        "age": ("health_profile", slice(0, 8)),
        "sex": ("health_profile", slice(8, 9)),
        "preexisting_conditions": ("health_profile", slice(9, 14)),
        "history_days": ("history_days", slice(None)),
        "valid_history_mask": ("valid_history_mask", slice(None)),
        "current_compartment": ("current_compartment", slice(None)),
        "infectiousness_history": ("infectiousness_history", slice(None)),
        "reported_symptoms_at_encounter": ("encounter_health", slice(0, 12)),
        "test_results_at_encounter": ("encounter_health", slice(12, 13)),
        "encounter_message": ("encounter_message", slice(None)),
        "encounter_partner_id": ("encounter_partner_id", slice(None)),
        "encounter_duration": ("encounter_duration", slice(None)),
        "encounter_day": ("encounter_day", slice(None)),
        "encounter_is_contagion": ("encounter_is_contagion", slice(None)),
    }

    # Compat with previous versions of the dataset
    DEFAULT_AGE = 0
    ASSUMED_MAX_AGE = 100
    ASSUMED_MIN_AGE = 1
    AGE_NOT_AVAILABLE = 0
    DEFAULT_SEX = 0
    DEFAULT_ENCOUNTER_DURATION = 10
    DEFAULT_PREEXISTING_CONDITIONS = [0.0, 0.0, 0.0, 0.0, 0.0]

    def __init__(
        self,
        path: str,
        relative_days=True,
        bit_encoded_age=True,
        clip_history_days=True,
        preload=False,
    ):
        """
        Parameters
        ----------
        path : str
            Path to the pickle file.
        relative_days : bool
            If set to True, the time-stamps (as days) are formatted such that
            the current day is represented as 0. Previous days are represented
            as negative values, i.e. day = -2 means the day before yesterday.
            If set to False, the time-stamps show the true number of days since
            day 0 (e.g. "today" can be represented as say 15).
        bit_encoded_age : bool
            Whether the age is to be encoded as a vector of bits or as a
            float between (0, 1). Invalid ages are encoded as -1.
        clip_history_days : bool
            Whether `history_days` (in the output) clips to the equivalent of
            day 0 for days that predate records. E.g. in day 5, whether
            `history_days[10]` is -5 (True) or -10 (False).
        preload : bool
            If path points to a zipfile, load the entire file to RAM (as a
            BytesIO object).
        """
        # Private
        self._num_id_bits = 16
        # Public
        self.path = path
        self.relative_days = relative_days
        self.bit_encoded_age = bit_encoded_age
        self.clip_history_days = clip_history_days
        self.preload = preload
        # Prepwork
        self._preloaded = None
        self._read_data()

    def _read_data(self):
        if os.path.isdir(self.path):
            # Case 1: Extracted files
            files = glob.glob(os.path.join(self.path, "*"))
            day_idxs, human_idxs = zip(
                *[
                    [
                        int(component)
                        for component in os.path.basename(file).strip(".pkl").split("-")
                    ]
                    for file in files
                ]
            )
        elif self.path.endswith(".zip"):
            # Case 2: Zipfiles
            if self.preload:
                with open(self.path, "rb") as f:
                    buffer = io.BytesIO(f.read())
                buffer.seek(0)
                self._preloaded = zipfile.ZipFile(buffer)
                files = self._preloaded.namelist()
            else:
                with zipfile.ZipFile(self.path) as zf:
                    files = zipfile.ZipFile.namelist(zf)
            # FIXME This needs more fixing
            day_idxs, human_idxs = zip(
                *[
                    [int(component) for component in file[:-4].split("-")]
                    for file in files
                    if (file.endswith(".pkl") and not file.startswith("__MACOSX"))
                ]
            )
        else:
            raise ValueError
        self._num_days = max(day_idxs) + 1
        self._num_humans = max(human_idxs)

    @property
    def num_humans(self):
        return self._num_humans

    @property
    def num_days(self):
        return self._num_days

    def __len__(self):
        return self.num_humans * self.num_days

    def read(self, human_idx, day_idx):
        file_name = f"{day_idx}-{human_idx + 1}.pkl"
        if os.path.isdir(self.path):
            # We're working with a dir, so we read and return
            file_name = os.path.join(self.path, file_name)
            with open(file_name, "rb") as f:
                return pickle.load(f)
        elif self.path.endswith(".zip"):
            # We're working with a zipfile
            # Check if we have the content preload to RAM
            if self._preloaded is not None:
                self._preloaded: zipfile.ZipFile
                with self._preloaded.open(file_name, "rb") as f:
                    return pickle.load(f)
            else:
                # Read zip archive from path, and then read content
                # from archive
                with zipfile.ZipFile(self.path) as zf:
                    with zf.open(file_name, "r") as f:
                        return pickle.load(f)

    def get(self, human_idx: int, day_idx: int, human_day_info: dict = None) -> Dict:
        """
        Parameters
        ----------
        human_idx : int
            Index specifying the human
        day_idx : int
            Index of the day
        human_day_info : dict
            If provided, use this dictionary instead of the content of the
            pickle file (which is read from file).

        Returns
        -------
        Dict
            An addict with the following attributes:
                -> `health_history`: 14-day health history of self of shape (14, 13)
                        with channels `reported_symptoms` (12), `test_results`(1).
                -> `health_profile`: health profile of the individual, 
                    of shape (14,) or (7,) depending on whether `bit_encoded_age` is 
                    set to True (former) or not (latter). If `bit_encoded_age`: 
                        We have  channels `age` (8), `sex` (1), and
                        `preexisting_conditions` (5,). The `age` has 8 channels because
                        we represent the corresponding integer in 8-bit binary. If the
                        age is not available (= 0), it is represented as a size-8 vector
                        of -1.
                    If not: 
                        Same as above, but `age` is now simply a float taking values in
                        Union([0, 1], {-1}). 0 corresponds to age 1 and 1 to age 100, 
                        whereas {-1} corresponds to the case where age is not available.
                -> `human_idx`: the ID of the human individual, of shape (1,). If not
                    available, it's set to -1.
                -> `day_idx`: the day from which the sample originates, of shape (1,).
                -> `history_days`: time-stamps to go with the health_history,
                    of shape (14, 1).
                -> `valid_history_mask`: 1 if the time-stamp corresponds to a valid
                    point in history, 0 otherwise, of shape (14,).
                -> `current_compartment`: current epidemic compartment (S/E/I/R)
                    of shape (4,).
                -> `infectiousness_history`: 14-day history of infectiousness,
                    of shape (14, 1).
                -> `encounter_health`: health during encounter, of shape (M, 13)
                -> `encounter_message`: risk transmitted during encounter, of shape (M, 8).
                        These are the 8 bits of info that can be sent between users.
                -> `encounter_partner_id`: id of the other in the encounter,
                        of shape (M, num_id_bits). If num_id_bits = 16, it means that the
                        id (say 65535) is represented in 16-bit binary.
                -> `encounter_duration`: duration of encounter, of shape (M, 1)
                -> `encounter_day`: the day of encounter, of shape (M, 1)
                -> `encounter_is_contagion`: whether the encounter was a contagion.
        """
        if human_day_info is None:
            human_day_info = self.read(human_idx, day_idx)
        else:
            day_idx = human_day_info["current_day"]
        if human_idx is None:
            human_idx = -1
        # -------- Encounters --------
        # Extract info about encounters
        #   encounter_info.shape = M3, where M is the number of encounters.
        encounter_info = human_day_info["observed"]["candidate_encounters"]
        # FIXME This is a hack:
        #  Filter encounter_info
        if encounter_info.size == 0:
            raise InvalidSetSize
        valid_encounter_mask = encounter_info[:, 2] > (day_idx - 14)
        encounter_info = encounter_info[valid_encounter_mask]
        # Check again
        if encounter_info.size == 0:
            raise InvalidSetSize
        if encounter_info.shape[1] == 3:
            encounter_partner_id, encounter_message, encounter_day = (
                encounter_info[:, 0],
                encounter_info[:, 1],
                encounter_info[:, 2],
            )
            # encounter_duration is not available in this version, so we use
            # a default constant of 10 minutes. The network shouldn't care.
            encounter_duration = (
                np.zeros(shape=(encounter_info.shape[0], 1))
                + self.DEFAULT_ENCOUNTER_DURATION
            )
        elif encounter_info.shape[1] == 4:
            (
                encounter_partner_id,
                encounter_message,
                encounter_duration,
                encounter_day,
            ) = (
                encounter_info[:, 0],
                encounter_info[:, 1],
                encounter_info[:, 2],
                encounter_info[:, 3],
            )
        else:
            raise ValueError
        num_encounters = encounter_info.shape[0]
        # Convert partner-id's to binary (shape = (M, num_id_bits))
        encounter_partner_id = (
            np.unpackbits(
                encounter_partner_id.astype(f"uint{self._num_id_bits}").view("uint8")
            )
            .reshape(num_encounters, -1)
            .astype("float32")
        )
        # Convert risk
        encounter_message = (
            np.unpackbits(encounter_message.astype("uint8"))
            .reshape(num_encounters, -1)
            .astype("float32")
        )
        encounter_is_contagion = human_day_info["unobserved"]["exposure_encounter"][
            valid_encounter_mask, None
        ].astype("float32")
        encounter_day = encounter_day.astype("float32")
        # -------- Health --------
        # Get health info
        health_history = np.concatenate(
            [
                human_day_info["observed"]["reported_symptoms"],
                human_day_info["observed"]["test_results"][:, None],
            ],
            axis=1,
        )
        infectiousness_history = human_day_info["unobserved"]["infectiousness"][:, None]
        history_days = np.arange(day_idx - 13, day_idx + 1)[::-1, None]
        valid_history_mask = (history_days >= 0)[:, 0]
        # Get historical health info given the day of encounter (shape = (M, 13))
        encounter_at_historical_day_idx = np.argmax(
            encounter_day == history_days, axis=0
        )
        health_at_encounter = health_history[encounter_at_historical_day_idx, :]
        if human_day_info["unobserved"]["is_recovered"]:
            current_compartment = "R"
        elif human_day_info["unobserved"]["is_infectious"]:
            current_compartment = "I"
        elif human_day_info["unobserved"]["is_exposed"]:
            current_compartment = "E"
        else:
            current_compartment = "S"
        current_compartment = np.array(
            [
                current_compartment == "S",
                current_compartment == "E",
                current_compartment == "I",
                current_compartment == "R",
            ]
        ).astype("float32")
        # Get age and sex if available, else use a default
        age = self._fetch_age(human_day_info)
        sex = np.array([human_day_info["observed"].get("sex", self.DEFAULT_SEX)])
        preexsting_conditions = human_day_info["observed"].get(
            "preexisting_conditions", np.array(self.DEFAULT_PREEXISTING_CONDITIONS)
        )
        health_profile = np.concatenate([age, sex, preexsting_conditions])
        # Clip history days if required
        if self.clip_history_days:
            history_days = np.clip(history_days, 0, None)
        # Normalize both days to assign 0 to present
        if self.relative_days:
            history_days = history_days - day_idx
            encounter_day = encounter_day - day_idx
        # This should be it
        return Dict(
            human_idx=torch.from_numpy(np.array([human_idx])),
            day_idx=torch.from_numpy(np.array([day_idx])),
            health_history=torch.from_numpy(health_history).float(),
            health_profile=torch.from_numpy(health_profile).float(),
            infectiousness_history=torch.from_numpy(infectiousness_history).float(),
            history_days=torch.from_numpy(history_days).float(),
            valid_history_mask=torch.from_numpy(valid_history_mask).float(),
            current_compartment=torch.from_numpy(current_compartment).float(),
            encounter_health=torch.from_numpy(health_at_encounter).float(),
            encounter_message=torch.from_numpy(encounter_message).float(),
            encounter_partner_id=torch.from_numpy(encounter_partner_id).float(),
            encounter_day=torch.from_numpy(encounter_day[:, None]).float(),
            encounter_duration=torch.from_numpy(encounter_duration[:, None]).float(),
            encounter_is_contagion=torch.from_numpy(encounter_is_contagion).float(),
        )

    def _fetch_age(self, human_day_info):
        age = human_day_info["observed"].get("age", self.DEFAULT_AGE)
        if self.bit_encoded_age:
            if age == 0:
                age = np.array([-1] * 8).astype("int")
            else:
                age = np.unpackbits(np.array([age]).astype("uint8")).astype("int")
        else:
            if age == 0:
                age = np.array([-1.0])
            else:
                age = (age - self.ASSUMED_MIN_AGE) / (
                    self.ASSUMED_MAX_AGE - self.ASSUMED_MIN_AGE
                )
                age = np.array([age])
        return age

    def __getitem__(self, item):
        human_idx, day_idx = np.unravel_index(item, (self.num_humans, self.num_days))
        while True:
            try:
                return self.get(human_idx, day_idx)
            except InvalidSetSize:
                # Try another day
                day_idx = (day_idx + 1) % self.num_days

    @classmethod
    def collate_fn(cls, batch):
        fixed_size_collates = {
            key: torch.stack([x[key] for x in batch], dim=0)
            for key in batch[0].keys()
            if key not in cls.SET_VALUED_FIELDS
        }

        # Make a mask
        max_set_len = max([x[cls.SET_VALUED_FIELDS[0]].shape[0] for x in batch])
        set_lens = torch.tensor([x[cls.SET_VALUED_FIELDS[0]].shape[0] for x in batch])
        mask = (
            torch.arange(max_set_len, dtype=torch.long)
            .expand(len(batch), max_set_len)
            .lt(set_lens[:, None])
        ).float()
        # Pad the set elements by writing in place to pre-made tensors
        padded_collates = {
            key: pad_sequence([x[key] for x in batch], batch_first=True)
            for key in cls.SET_VALUED_FIELDS
        }
        # Make the final addict and return
        collates = Dict(mask=mask)
        collates.update(fixed_size_collates)
        collates.update(padded_collates)
        return collates

    @classmethod
    def extract(
        cls,
        tensor_or_dict: Union[torch.Tensor, dict],
        query_field: str,
        tensor_name: str = None,
    ) -> torch.Tensor:
        """
        This function can do two things.
            1. Given a dict (output from __getitem__/get or from collate_fn),
               extract the field given by name `query_fields`.
            2. Given a tensor and a `tensor_name`, assume that the tensor originated
               by indexing the dictionary returned by `__getitem__`/`get`/`collate_fn`
               with `tensor_name`. Now, proceed to extract the field given by name
               `query_fields`.
        Parameters
        ----------
        tensor_or_dict : torch.Tensor or dict
            Torch tensor or dictionary.
        query_field : str
            Name of the field to extract.
        tensor_name : str
            If `tensor_or_dict` is a torch tensor, assume this is the dictionary
            key that was used to obtain the said tensor. Can be set to None if
            tensor_or_dict is a dict, but if not, it will be validated.

        Returns
        -------
        torch.Tensor
        """
        assert query_field in cls.INPUT_FIELD_TO_SLICE_MAPPING
        if isinstance(tensor_or_dict, dict):
            tensor_name, slice_ = cls.INPUT_FIELD_TO_SLICE_MAPPING[query_field]
            tensor = tensor_or_dict[tensor_name]
        elif torch.is_tensor(tensor_or_dict):
            target_tensor_name, slice_ = cls.INPUT_FIELD_TO_SLICE_MAPPING[query_field]
            if tensor_name is not None:
                assert target_tensor_name == tensor_name
            tensor = tensor_or_dict
        else:
            raise TypeError
        return tensor[..., slice_]

    def __del__(self):
        if self._preloaded is not None:
            self._preloaded.close()


class ContactPreprocessor(ContactDataset):
    def __init__(self, **kwargs):
        # noinspection PyTypeChecker
        super(ContactPreprocessor, self).__init__(path=None, **kwargs)
        self._num_humans = 1
        self._num_days = 1

    def _read_data(self):
        # Defuse this method since it's not needed anymore
        pass

    def preprocess(self, human_day_info, as_batch=True):
        # noinspection PyTypeChecker
        sample = self.get(None, None, human_day_info=human_day_info)
        if as_batch:
            sample = self.collate_fn([sample])
        return sample

    def __len__(self):
        raise NotImplementedError

    def __getitem__(self, item):
        raise NotImplementedError


def get_dataloader(batch_size, shuffle=True, num_workers=1, **dataset_kwargs):
    path = dataset_kwargs.pop("path")
    if isinstance(path, str):
        dataset = ContactDataset(path=path, **dataset_kwargs)
    elif isinstance(path, (list, tuple)):
        dataset = ConcatDataset(
            [ContactDataset(path=p, **dataset_kwargs) for p in path]
        )
    else:
        raise TypeError
    dataloader = DataLoader(
        dataset=dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        num_workers=num_workers,
        collate_fn=ContactDataset.collate_fn,
    )
    return dataloader
