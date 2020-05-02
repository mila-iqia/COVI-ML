from typing import Union
from contextlib import contextmanager

import torch
import torch.nn as nn

import modules as mods
import attn

# Legend
#   B: batch (i.e. humans)
#   T: index of the day
#   M: message/encounter index
#   C: channel


class _ContactTracingTransformer(nn.Module):
    def __init__(
        self,
        *,
        health_history_embedding: nn.Module,
        health_profile_embedding: nn.Module,
        time_embedding: nn.Module,
        duration_embedding: nn.Module,
        partner_id_embedding: Union[nn.Module, None],
        message_embedding: nn.Module,
        self_attention_blocks: nn.ModuleList,
        latent_variable_mlp: nn.Module,
        encounter_mlp: nn.Module,
        entity_masker: nn.Module,
        message_placeholder: nn.Parameter,
        partner_id_placeholder: nn.Parameter,
        duration_placeholder: nn.Parameter,
    ):
        super(_ContactTracingTransformer, self).__init__()
        # Private
        self._diagnose = False
        # Public
        self.health_history_embedding = health_history_embedding
        self.health_profile_embedding = health_profile_embedding
        self.time_embedding = time_embedding
        self.duration_embedding = duration_embedding
        self.partner_id_embedding = partner_id_embedding
        self.message_embedding = message_embedding
        self.self_attention_blocks = self_attention_blocks
        self.latent_variable_mlp = latent_variable_mlp
        self.encounter_mlp = encounter_mlp
        self.entity_masker = entity_masker
        self.message_placeholder = message_placeholder
        self.partner_id_placeholder = partner_id_placeholder
        self.duration_placeholder = duration_placeholder

    @contextmanager
    def diagnose(self):
        old_diagnose = self._diagnose
        self._diagnose = True
        yield
        self._diagnose = old_diagnose

    def forward(self, inputs):
        """
        inputs is a dict containing the below keys. The format of the tensors
        are indicated as e.g. `BTC`, `BMC` (etc), which can be interpreted as
        following.
            B: batch size,
            T: size of the rolling window over health history (i.e. number of
               time-stamps),
            C: number of generic channels,
            M: number of encounters,
        Elements with pre-determined shapes are indicated as such.
        For example:
            - B(14) indicates a tensor of shape (B, 14),
            - BM1 indicates a tensor of shape (B, M, 1)
            - B(T=14)C indicates a tensor of shape (B, 14, C) where 14
                is the currently set size of the rolling window.

        Parameters
        ----------
        inputs : dict
            A python dict with the following keys:
                -> `health_history`: a B(T=14)C tensor of the 14-day health
                    history (symptoms + test results + day) of the individual.
                -> `health_profile`: a BC tensor of the health profile
                    containing (age + health + preexisting_conditions) of the
                    individual.
                -> `history_days`: a B(T=14)1 tensor of the day corresponding to the
                    T dimension in `health_history`.
                -> `encounter_health`: a BMC tensor of health during an
                    encounter indexed by M.
                -> `encounter_message`: a BMC tensor of the received
                    message from the encounter partner.
                -> `encounter_day`: a BM1 tensor of the encounter day.
                -> `encounter_duration`: a BM1 tensor of the encounter duration.
                    This is not the actual duration, but a proxy (for the number
                    of encounters)
                -> `encounter_partner_id`: a binary  BMC tensor specifying
                    the ID of the encounter partner.
                -> `mask`: a BM mask tensor distinguishing the valid entries (1)
                    from padding (0) in the set valued inputs.
                -> `valid_history_mask`: a B(14) mask tensor distinguising valid
                    points in history (1) from padding (0).
        Returns
        -------
        dict
        """
        # -------- Shape Wrangling --------
        batch_size = inputs["health_history"].shape[0]
        num_history_days = inputs["health_history"].shape[1]
        num_encounters = inputs["encounter_health"].shape[1]
        # -------- Embeddings --------
        # Embed health history
        embedded_health_history = self.health_history_embedding(
            inputs["health_history"], inputs["valid_history_mask"]
        )
        embedded_health_profile = self.health_profile_embedding(
            inputs["health_profile"]
        )
        embedded_encounter_health = self.health_history_embedding(
            inputs["encounter_health"], inputs["mask"]
        )
        # Embed time (days and duration)
        embedded_history_days = self.time_embedding(
            inputs["history_days"], inputs["valid_history_mask"]
        )
        embedded_encounter_day = self.time_embedding(
            inputs["encounter_day"], inputs["mask"]
        )
        embedded_encounter_duration = self.duration_embedding(
            inputs["encounter_duration"], inputs["mask"]
        )
        # Embed partner-IDs
        if self.partner_id_embedding is not None:
            embedded_encounter_partner_ids = self.partner_id_embedding(
                inputs["encounter_partner_id"], inputs["mask"]
            )
        else:
            embedded_encounter_partner_ids = self.partner_id_placeholder[
                None, None
            ].expand(batch_size, num_encounters, self.partner_id_placeholder.shape[-1])
            embedded_encounter_partner_ids = self.entity_masker(
                embedded_encounter_partner_ids, inputs["mask"]
            )
        # Embed messages
        embedded_encounter_messages = self.message_embedding(
            inputs["encounter_message"], inputs["mask"]
        )
        # -------- Self Attention --------
        # Prepare the entities -- one set for the encounters and the other for self health
        # Before we start, expand health profile from BC to BMC and append to entities
        expanded_health_profile_per_encounter = embedded_health_profile[
            :, None, :
        ].expand(batch_size, num_encounters, embedded_health_profile.shape[-1])
        encounter_entities = torch.cat(
            [
                embedded_encounter_day,
                embedded_encounter_partner_ids,
                embedded_encounter_duration,
                embedded_encounter_health,
                embedded_encounter_messages,
                expanded_health_profile_per_encounter,
            ],
            dim=-1,
        )
        # Expand the messages and placeholders from C to BTC
        expanded_message_placeholder = self.message_placeholder[None, None].expand(
            batch_size, num_history_days, embedded_encounter_messages.shape[-1]
        )
        expanded_pid_placeholder = self.partner_id_placeholder[None, None].expand(
            batch_size, num_history_days, embedded_encounter_partner_ids.shape[-1]
        )
        expanded_duration_placeholder = self.duration_placeholder[None, None].expand(
            batch_size, num_history_days, embedded_encounter_duration.shape[-1]
        )
        # Expand the health profile from C to BTC
        expanded_health_profile_per_day = embedded_health_profile[:, None, :].expand(
            batch_size, num_history_days, embedded_health_profile.shape[-1]
        )
        self_entities = torch.cat(
            [
                embedded_history_days,
                expanded_pid_placeholder,
                expanded_duration_placeholder,
                embedded_health_history,
                expanded_message_placeholder,
                expanded_health_profile_per_day,
            ],
            dim=-1,
        )
        # Concatenate encounter and self entities in to one big set (before passing to
        # the self attention blocks). In addition, expand inputs.mask to account for
        # masking the entire set of entities.
        entities = torch.cat([encounter_entities, self_entities], dim=1)
        expanded_mask = torch.cat([inputs["mask"], inputs["valid_history_mask"]], dim=1)
        entities = self.entity_masker(entities, expanded_mask)
        # Grab a copy of the "meta-data", which we will be appending to entities at
        # every step. These meta-data are the time-stamps and partner_ids
        meta_data_dim = (
            embedded_history_days.shape[2]
            + embedded_encounter_partner_ids.shape[2]
            + embedded_encounter_duration.shape[2]
        )
        meta_data = entities[:, :, :meta_data_dim]
        # Make a mask for the attention mech. This mask prevents attention between
        # two entities if either one of them is a padding entity.
        attention_mask = expanded_mask[:, :, None] * expanded_mask[:, None, :]
        # Let'er rip!
        # noinspection PyTypeChecker
        for sab in self.self_attention_blocks:
            entities = sab(entities, weights=attention_mask)
            entities = self.entity_masker(entities, expanded_mask)
            # Append meta-data for the next round of message passing
            entities = torch.cat([meta_data, entities], dim=2)
        # -------- Latent Variables
        pre_latent_variable = entities[:, num_encounters:]
        # Push through the latent variable MLP to get the latent variables
        # latent_variable.shape = BTC
        latent_variable = self.latent_variable_mlp(pre_latent_variable)
        # -------- Generate Output Variables --------
        # Process encounters to their variables
        pre_encounter_variables = entities[:, :num_encounters, meta_data_dim:]
        encounter_variables = self.encounter_mlp(pre_encounter_variables)
        # Done: pack to an addict and return
        results = dict()
        results["encounter_variables"] = encounter_variables
        results["latent_variable"] = latent_variable
        if self._diagnose:
            _locals = dict(locals())
            _locals.pop("results")
            _locals.pop("self")
            _locals.pop("encounter_variables")
            _locals.pop("latent_variable")
            results.update(_locals)
        return results


class ContactTracingTransformer(_ContactTracingTransformer):
    def __init__(
        self,
        *,
        # Embeddings
        capacity=128,
        dropout=0.1,
        num_health_history_features=29,
        health_history_embedding_dim=64,
        num_health_profile_features=12,
        health_profile_embedding_dim=32,
        use_learned_time_embedding=True,
        time_embedding_dim=32,
        encounter_duration_embedding_dim=32,
        encounter_duration_embedding_mode="sines",
        encounter_duration_thermo_range=(0.0, 6.0),
        encounter_duration_num_thermo_bins=32,
        num_encounter_partner_id_bits=16,
        use_encounter_partner_id_embedding=False,
        encounter_partner_id_embedding_dim=32,
        message_dim=8,
        message_embedding_dim=128,
        # Attention
        num_heads=4,
        sab_capacity=128,
        num_sabs=2,
        # Output
        encounter_output_features=1,
        latent_variable_output_features=1,
    ):
        # ------- Embeddings -------
        health_history_embedding = mods.HealthHistoryEmbedding(
            in_features=num_health_history_features,
            embedding_size=health_history_embedding_dim,
            capacity=capacity,
            dropout=dropout,
        )
        health_profile_embedding = mods.HealthProfileEmbedding(
            in_features=num_health_profile_features,
            embedding_size=health_profile_embedding_dim,
            capacity=capacity,
            dropout=dropout,
        )
        if use_learned_time_embedding:
            time_embedding = mods.TimeEmbedding(embedding_size=time_embedding_dim)
        else:
            time_embedding = mods.PositionalEncoding(encoding_dim=time_embedding_dim)
        if encounter_duration_embedding_mode == "thermo":
            duration_embedding = mods.DurationEmbedding(
                num_thermo_bins=encounter_duration_num_thermo_bins,
                embedding_size=encounter_duration_embedding_dim,
                thermo_range=encounter_duration_thermo_range,
                capacity=capacity,
                dropout=dropout,
            )
        elif encounter_duration_embedding_mode == "sines":
            duration_embedding = mods.PositionalEncoding(
                encoding_dim=encounter_duration_embedding_dim
            )
        else:
            raise ValueError
        if use_encounter_partner_id_embedding:
            partner_id_embedding = mods.PartnerIdEmbedding(
                num_id_bits=num_encounter_partner_id_bits,
                embedding_size=encounter_partner_id_embedding_dim,
            )
        else:
            partner_id_embedding = None
        message_embedding = mods.MessageEmbedding(
            message_dim=message_dim,
            embedding_size=message_embedding_dim,
            capacity=capacity,
            dropout=dropout,
        )
        # ------- Attention -------
        sab_in_dim = (
            time_embedding_dim
            + encounter_partner_id_embedding_dim
            + encounter_duration_embedding_dim
            + health_history_embedding_dim
            + message_embedding_dim
            + health_profile_embedding_dim
        )
        sab_metadata_dim = (
            time_embedding_dim
            + encounter_partner_id_embedding_dim
            + encounter_duration_embedding_dim
        )
        sab_intermediate_in_dim = sab_capacity + sab_metadata_dim
        # Build the SABs
        if num_sabs >= 1:
            self_attention_blocks = [
                attn.SAB(dim_in=sab_in_dim, dim_out=sab_capacity, num_heads=num_heads)
            ]
        else:
            # This is a special code-path where we don't use any self-attention,
            # but just a plain-old MLP (as a baseline).
            self_attention_blocks = [
                nn.Sequential(
                    nn.Linear(sab_in_dim, sab_capacity),
                    nn.ReLU(),
                    nn.Linear(sab_capacity, sab_capacity),
                    nn.ReLU(),
                    nn.Linear(sab_capacity, sab_capacity),
                    nn.ReLU(),
                )
            ]
        for sab_idx in range(num_sabs - 1):
            self_attention_blocks.append(
                attn.SAB(
                    dim_in=sab_intermediate_in_dim,
                    dim_out=sab_capacity,
                    num_heads=num_heads,
                )
            )
        self_attention_blocks = nn.ModuleList(self_attention_blocks)
        # ------- Output processors -------
        # Encounter
        encounter_mlp = nn.Sequential(
            nn.Linear(sab_capacity, capacity),
            nn.ReLU(),
            nn.Linear(capacity, encounter_output_features),
        )
        # Latent variables
        latent_variable_mlp = nn.Sequential(
            nn.Linear(sab_capacity + sab_metadata_dim, capacity),
            nn.ReLU(),
            nn.Linear(capacity, latent_variable_output_features),
        )
        # ------- Output placeholders -------
        # noinspection PyArgumentList
        message_placeholder = nn.Parameter(torch.randn((message_embedding_dim,)))
        # noinspection PyArgumentList
        partner_id_placeholder = nn.Parameter(
            torch.randn((encounter_partner_id_embedding_dim,))
        )
        # noinspection PyArgumentList
        duration_placeholder = nn.Parameter(
            torch.randn((encounter_duration_embedding_dim,))
        )
        # ------- Masking -------
        entity_masker = mods.EntityMasker()
        # Done; init the super
        super(ContactTracingTransformer, self).__init__(
            health_history_embedding=health_history_embedding,
            health_profile_embedding=health_profile_embedding,
            time_embedding=time_embedding,
            duration_embedding=duration_embedding,
            partner_id_embedding=partner_id_embedding,
            message_embedding=message_embedding,
            self_attention_blocks=self_attention_blocks,
            latent_variable_mlp=latent_variable_mlp,
            encounter_mlp=encounter_mlp,
            entity_masker=entity_masker,
            message_placeholder=message_placeholder,
            partner_id_placeholder=partner_id_placeholder,
            duration_placeholder=duration_placeholder,
        )


if __name__ == '__main__':
    model = ContactTracingTransformer()
    torch.save(model.state_dict(),"./model.pth")
