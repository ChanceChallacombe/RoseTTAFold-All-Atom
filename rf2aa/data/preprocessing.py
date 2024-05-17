import os
import shutil
from hydra import initialize, compose
from pathlib import Path
import subprocess

#from rf2aa.run_inference import ModelRunner


def make_msa(
    fasta_file,
    chain,
    model_runner
): 
    out_dir_base = Path(model_runner.config.output_path)
    hash = model_runner.config.job_name
     # out_dir = out_dir_base / hash / chain  # TODO: cache MSAs
    out_dir = out_dir_base / "msa_results" / chain

    shutil.rmtree(out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)

    command = model_runner.config.database_params.command
    ur30_search_base = model_runner.config.database_params.sequencedb_ur30
    bfd_search_base = model_runner.config.database_params.sequencedb_bfd

    num_cpus = model_runner.config.database_params.num_cpus
    ram_gb = model_runner.config.database_params.mem
    template_database = model_runner.config.database_params.hhdb

    out_a3m = out_dir / "t000_.msa0.a3m"
    out_atab = out_dir / "t000_.atab"
    out_hhr = out_dir / "t000_.hhr"

    #if out_a3m.exists() and out_atab.exists() and out_hhr.exists():
    #    return out_a3m, out_hhr, out_atab

    search_command = f"./{command} {fasta_file} {out_dir} {num_cpus} {ram_gb} {template_database} {ur30_search_base} {bfd_search_base}"
    print(search_command)
    _ = subprocess.run(search_command, shell=True)
    return out_a3m, out_hhr, out_atab

