import os
import hydra
from hydra import initialize, compose
from omegaconf import OmegaConf, open_dict
import torch
import numpy as np
import torch.nn as nn
from dataclasses import asdict
from pathlib import Path

from rf2aa.data.merge_inputs import merge_all
from rf2aa.data.covale import load_covalent_molecules
from rf2aa.data.nucleic_acid import load_nucleic_acid
from rf2aa.data.protein import generate_msa_and_load_protein
from rf2aa.data.small_molecule import load_small_molecule
from rf2aa.ffindex import *
from rf2aa.chemical import initialize_chemdata, load_pdb_ideal_sdf_strings
from rf2aa.chemical import ChemicalData as ChemData
from rf2aa.model.RoseTTAFoldModel import RoseTTAFoldModule
from rf2aa.training.recycling import recycle_step_legacy
from rf2aa.util import writepdb, is_atom, Ls_from_same_chain_2d
from rf2aa.util_module import XYZConverter


class ModelManager:

    def __init__(self, device, checkpoint_path, checkpoint_weights, template_db, sequencedb_ur30, sequencedb_bfd,
                 msa_command) -> None:
        self.config = self.load_config(checkpoint_path, sequencedb_ur30, sequencedb_bfd, template_db, msa_command)

        initialize_chemdata(self.config.chem_params)
        FFindexDB = namedtuple("FFindexDB", "index, data")
        self.ffdb = FFindexDB(read_index(self.config.database_params.hhdb + '_pdb.ffindex'),
                              read_data(self.config.database_params.hhdb + '_pdb.ffdata'))
        self.device = device
        self.xyz_converter = XYZConverter()
        self.deterministic = self.config.get("deterministic", False)
        self.molecule_db = load_pdb_ideal_sdf_strings()
        self.load_model(checkpoint_path / checkpoint_weights)

    @staticmethod
    def load_config(checkpoint_path, sequencedb_ur30, sequencedb_bfd, template_db, msa_command):
        config_path = 'config/inference'
        # Initialize Hydra context
        with initialize(config_path=config_path):
            # Compose the config based specifically on the 'base' configuration
            cfg = compose(
                config_name="base")
        with open_dict(cfg):
            cfg.database_params.sequencedb_ur30 = str(checkpoint_path / sequencedb_ur30)
            cfg.database_params.sequencedb_bfd = str(checkpoint_path / sequencedb_bfd)
            cfg.database_params.hhdb = str(checkpoint_path / template_db)
            cfg.database_params.command = msa_command
        return cfg

    def parse_inference_config(self, input_config):
        residues_to_atomize = []  # chain letter, residue number, residue name
        chains = []
        protein_inputs = {}
        if input_config.protein_inputs is not None:
            for chain in input_config.protein_inputs:
                if chain in chains:
                    raise ValueError(f"Duplicate chain found with name: {chain}. Please specify unique chain names")
                elif len(chain) > 1:
                    raise ValueError(f"Chain name must be a single character, found chain with name: {chain}")
                else:
                    chains.append(chain)
                protein_input = generate_msa_and_load_protein(
                    input_config.protein_inputs[chain]["fasta_file"],
                    chain,
                    self
                )
                protein_inputs[chain] = protein_input

        na_inputs = {}
        if input_config.na_inputs is not None:
            for chain in input_config.na_inputs:
                na_input = load_nucleic_acid(
                    input_config.na_inputs[chain]["fasta"],
                    input_config.na_inputs[chain]["input_type"],
                    self
                )
                na_inputs[chain] = na_input

        sm_inputs = {}
        # first if any of the small molecules are covalently bonded to the protein
        # merge the small molecule with the residue and add it as a separate ligand
        # also add it to residues_to_atomize for bookkeeping later on
        # need to handle atomizing multiple consecutive residues here too
        if input_config.covale_inputs is not None:
            covalent_sm_inputs, residues_to_atomize_covale = load_covalent_molecules(protein_inputs, self.config, self,
                                                                                     input_config)
            sm_inputs.update(covalent_sm_inputs)
            residues_to_atomize.extend(residues_to_atomize_covale)

        if input_config.sm_inputs is not None:
            for chain in input_config.sm_inputs:
                if input_config.sm_inputs[chain]["input_type"] not in ["smiles", "sdf"]:
                    raise ValueError("Small molecule input type must be smiles or sdf")
                if chain in sm_inputs:  # chain already processed as covale
                    continue
                if "is_leaving" in input_config.sm_inputs[chain]:
                    raise ValueError("Leaving atoms are not supported for non-covalently bonded molecules")
                sm_input = load_small_molecule(
                    input_config.sm_inputs[chain]["input"],
                    input_config.sm_inputs[chain]["input_type"],
                    self
                )
                sm_inputs[chain] = sm_input

        if self.config.residue_replacement is not None:
            # add to the sm_inputs list
            # add to residues to atomize
            raise NotImplementedError("Modres inference is not implemented")

        raw_data = merge_all(protein_inputs, na_inputs, sm_inputs, residues_to_atomize,
                             deterministic=self.deterministic)
        self.raw_data = raw_data

    def load_model(self, checkpoint_path):
        self.model = RoseTTAFoldModule(
            **self.config.legacy_model_param,
            aamask=ChemData().allatom_mask.to(self.device),
            atom_type_index=ChemData().atom_type_index.to(self.device),
            ljlk_parameters=ChemData().ljlk_parameters.to(self.device),
            lj_correction_parameters=ChemData().lj_correction_parameters.to(self.device),
            num_bonds=ChemData().num_bonds.to(self.device),
            cb_len=ChemData().cb_length_t.to(self.device),
            cb_ang=ChemData().cb_angle_t.to(self.device),
            cb_tor=ChemData().cb_torsion_t.to(self.device),

        ).to(self.device)
        checkpoint = torch.load(checkpoint_path, map_location=self.device)
        self.model.load_state_dict(checkpoint['model_state_dict'])

    def construct_features(self):
        return self.raw_data.construct_features(self)

    def run_model_forward(self, input_feats):
        input_feats.add_batch_dim()
        input_feats.to(self.device)
        input_dict = asdict(input_feats)
        input_dict["bond_feats"] = input_dict["bond_feats"].long()
        input_dict["seq_unmasked"] = input_dict["seq_unmasked"].long()
        outputs = recycle_step_legacy(self.model,
                                      input_dict,
                                      self.config.loader_params.MAXCYCLE,
                                      use_amp=False,
                                      nograds=True,
                                      force_device=self.device)
        return outputs

    def write_outputs(self, input_feats, outputs):
        logits, logits_aa, logits_pae, logits_pde, p_bind, \
            xyz, alpha_s, xyz_allatom, lddt, _, _, _ \
            = outputs
        seq_unmasked = input_feats.seq_unmasked
        bond_feats = input_feats.bond_feats
        err_dict = self.calc_pred_err(lddt, logits_pae, logits_pde, seq_unmasked)
        err_dict["same_chain"] = input_feats.same_chain
        plddts = err_dict["plddts"]
        Ls = Ls_from_same_chain_2d(input_feats.same_chain)
        plddts = plddts[0]
        writepdb(os.path.join(f"{self.config.output_path}", f"pdb_result.pdb"),
                 xyz_allatom,
                 seq_unmasked,
                 bond_feats=bond_feats,
                 bfacts=plddts,
                 chain_Ls=Ls
                 )

        with open(os.path.join(f"{self.config.output_path}", f"pdb_result.pdb"), 'r') as f:
            pdb_str = f.read()
        err_dict.pop("same_chain", None)
        err_dict = {key: value.detach().cpu().numpy().tolist() if isinstance(value, torch.Tensor) else value for
                    key, value in err_dict.items()}

        err_dict = {k: (v if not isinstance(v, float) or not np.isnan(v) else None) for k, v in err_dict.items()}

        return {"pdb": pdb_str, "error_dictionary": err_dict}

    def infer(self, input_config):
        self.parse_inference_config(input_config)
        input_feats = self.construct_features()
        outputs = self.run_model_forward(input_feats)
        return self.write_outputs(input_feats, outputs)

    def lddt_unbin(self, pred_lddt):
        # calculate lddt prediction loss
        nbin = pred_lddt.shape[1]
        bin_step = 1.0 / nbin
        lddt_bins = torch.linspace(bin_step, 1.0, nbin, dtype=pred_lddt.dtype, device=pred_lddt.device)

        pred_lddt = nn.Softmax(dim=1)(pred_lddt)
        return torch.sum(lddt_bins[None, :, None] * pred_lddt, dim=1)

    def pae_unbin(self, logits_pae, bin_step=0.5):
        nbin = logits_pae.shape[1]
        bins = torch.linspace(bin_step * 0.5, bin_step * nbin - bin_step * 0.5, nbin,
                              dtype=logits_pae.dtype, device=logits_pae.device)
        logits_pae = torch.nn.Softmax(dim=1)(logits_pae)
        return torch.sum(bins[None, :, None, None] * logits_pae, dim=1)

    def pde_unbin(self, logits_pde, bin_step=0.3):
        nbin = logits_pde.shape[1]
        bins = torch.linspace(bin_step * 0.5, bin_step * nbin - bin_step * 0.5, nbin,
                              dtype=logits_pde.dtype, device=logits_pde.device)
        logits_pde = torch.nn.Softmax(dim=1)(logits_pde)
        return torch.sum(bins[None, :, None, None] * logits_pde, dim=1)

    def calc_pred_err(self, pred_lddts, logit_pae, logit_pde, seq):
        """Calculates summary metrics on predicted lDDT and distance errors"""
        plddts = self.lddt_unbin(pred_lddts)
        pae = self.pae_unbin(logit_pae) if logit_pae is not None else None
        pde = self.pde_unbin(logit_pde) if logit_pde is not None else None
        sm_mask = is_atom(seq)[0]
        sm_mask_2d = sm_mask[None, :] * sm_mask[:, None]
        prot_mask_2d = (~sm_mask[None, :]) * (~sm_mask[:, None])
        inter_mask_2d = sm_mask[None, :] * (~sm_mask[:, None]) + (~sm_mask[None, :]) * sm_mask[:, None]
        # assumes B=1
        err_dict = dict(
            plddts=plddts.cpu(),
            pae=pae.cpu(),
            pde=pde.cpu(),
            mean_plddt=float(plddts.mean()),
            mean_pae=float(pae.mean()) if pae is not None else None,
            pae_prot=float(pae[0, prot_mask_2d].mean()) if pae is not None else None,
            pae_inter=float(pae[0, inter_mask_2d].mean()) if pae is not None else None,
        )
        return err_dict
