import argparse
import logging
from multiprocessing import Manager
import os
import random
from concurrent.futures import ProcessPoolExecutor
from glob import glob
from random import shuffle
from time import sleep
from log import logger
import librosa
import numpy as np
if __name__ == "__main__":
    logger.info("Loading torch, it may take a while...")
import torch
import torch.multiprocessing as mp
if __name__ == "__main__":
    logger.success("Loaded torch")

from tqdm import tqdm

# from log import logger

import diffusion.logger.utils as du
import utils
from diffusion.vocoder import Vocoder
from modules.mel_processing import spectrogram_torch

import rich_utils
from rich.live import Live

# from rich_utils.shared import live, progress

logging.getLogger("numba").setLevel(logging.WARNING)
logging.getLogger("matplotlib").setLevel(logging.WARNING)

hps = utils.get_hparams_from_file("configs/config.json")
dconfig = du.load_config("configs/diffusion.yaml")
sampling_rate = hps.data.sampling_rate
hop_length = hps.data.hop_length
speech_encoder = hps["model"]["speech_encoder"]

def process_one(filename, hmodel,f0p,rank,diff=False,mel_extractor=None):
    '''
    用于处理单个文件
    '''

    # print(filename)
    wav, sr = librosa.load(filename, sr=sampling_rate)
    audio_norm = torch.FloatTensor(wav)
    audio_norm = audio_norm.unsqueeze(0)
    device = torch.device(f"cuda:{rank}")
    soft_path = filename + ".soft.pt"
    if not os.path.exists(soft_path):
        wav16k = librosa.resample(wav, orig_sr=sampling_rate, target_sr=16000)
        wav16k = torch.from_numpy(wav16k).to(device)
        c = hmodel.encoder(wav16k)
        torch.save(c.cpu(), soft_path)

    f0_path = filename + ".f0.npy"
    if not os.path.exists(f0_path):
        f0_predictor = utils.get_f0_predictor(f0p,sampling_rate=sampling_rate, hop_length=hop_length,device=None,threshold=0.05)
        f0,uv = f0_predictor.compute_f0_uv(
            wav
        )
        np.save(f0_path, np.asanyarray((f0,uv),dtype=object))


    spec_path = filename.replace(".wav", ".spec.pt")
    if not os.path.exists(spec_path):
        # Process spectrogram
        # The following code can't be replaced by torch.FloatTensor(wav)
        # because load_wav_to_torch return a tensor that need to be normalized

        if sr != hps.data.sampling_rate:
            raise ValueError(
                "{} SR doesn't match target {} SR".format(
                    sr, hps.data.sampling_rate
                )
            )

        #audio_norm = audio / hps.data.max_wav_value

        spec = spectrogram_torch(
            audio_norm,
            hps.data.filter_length,
            hps.data.sampling_rate,
            hps.data.hop_length,
            hps.data.win_length,
            center=False,
        )
        spec = torch.squeeze(spec, 0)
        torch.save(spec, spec_path)

    if diff or hps.model.vol_embedding:
        volume_path = filename + ".vol.npy"
        volume_extractor = utils.Volume_Extractor(hop_length)
        if not os.path.exists(volume_path):
            volume = volume_extractor.extract(audio_norm)
            np.save(volume_path, volume.to('cpu').numpy())

    if diff:
        mel_path = filename + ".mel.npy"
        if not os.path.exists(mel_path) and mel_extractor is not None:
            mel_t = mel_extractor.extract(audio_norm.to(device), sampling_rate)
            mel = mel_t.squeeze().to('cpu').numpy()
            np.save(mel_path, mel)
        aug_mel_path = filename + ".aug_mel.npy"
        aug_vol_path = filename + ".aug_vol.npy"
        max_amp = float(torch.max(torch.abs(audio_norm))) + 1e-5
        max_shift = min(1, np.log10(1/max_amp))
        log10_vol_shift = random.uniform(-1, max_shift)
        keyshift = random.uniform(-5, 5)
        if mel_extractor is not None:
            aug_mel_t = mel_extractor.extract(audio_norm * (10 ** log10_vol_shift), sampling_rate, keyshift = keyshift)
        aug_mel = aug_mel_t.squeeze().to('cpu').numpy()
        aug_vol = volume_extractor.extract(audio_norm * (10 ** log10_vol_shift))
        if not os.path.exists(aug_mel_path):
            np.save(aug_mel_path,np.asanyarray((aug_mel,keyshift),dtype=object))
        if not os.path.exists(aug_vol_path):
            np.save(aug_vol_path,aug_vol.to('cpu').numpy())

def process_batch(q, file_chunk, f0p, diff=False, mel_extractor=None, fake_processing=False):
    ''''
    用于处理一个 batch 的文件
    '''

    # logger.info("Loading speech encoder for content...")
    # print(fake_processing)
    # exit(6)
    if not fake_processing:
        rank = mp.current_process()._identity
        rank = rank[0] if len(rank) > 0 else 0
        if torch.cuda.is_available():
            gpu_id = rank % torch.cuda.device_count()
            device = torch.device(f"cuda:{gpu_id}")
        # logger.info(f"Rank {rank} uses device {device}")
        hmodel = utils.get_speech_encoder(speech_encoder, device=device)
        # logger.success("Loaded speech encoder.")
    else:
        # logger.info("Skip load speech encoder")
        pass
    for filename in file_chunk:
        if not fake_processing:
            process_one(filename, hmodel, f0p, rank, diff, mel_extractor)
        else:
            sleep(random.random())
        # print("update taskid ", TaskID)
        # print()
        # progress.update(TaskID)
        q.put(6)

def parallel_process(filenames, num_processes, f0p, diff, mel_extractor, fake_processing):
    '''
    用于并行处理
    '''
    
    with ProcessPoolExecutor(max_workers=num_processes) as executor:
        tasks = []
        with Manager() as manager:
            queues = [manager.Queue() for _ in range(num_processes)]
            for i in range(num_processes):
                start = int(i * len(filenames) / num_processes)
                end = int((i + 1) * len(filenames) / num_processes)
                file_chunk = filenames[start:end]
                progress.add_task(len(file_chunk), f"Worker {i+1}")
                tasks.append(executor.submit(process_batch, queues[i], file_chunk, f0p, diff, mel_extractor, fake_processing))
            with Live(progress, refresh_per_second=10, transient=True) as live:
                while not progress.overall_progress.finished:
                    for i in range(num_processes):
                        # logger.info(f"{i}, {queues[i].qsize()}")
                        qsize = queues[i].qsize()
                        progress.update(i,value=queues[i].qsize())
                    sleep(0.5)
if __name__ == "__main__":

    global progress
    progress = rich_utils.MProgress("preprocessing f0 and hubert", "workers")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--in_dir", type=str, default="dataset/44k", help="path to input dir"
    )
    parser.add_argument(
        '--use_diff',action='store_true', help='Whether to use the diffusion model'
    )
    parser.add_argument(
        '--f0_predictor', type=str, default="dio", help='Select F0 predictor, can select crepe,pm,dio,harvest,rmvpe, default pm(note: crepe is original F0 using mean filter)'
    )
    parser.add_argument(
        '--num_processes', type=int, default=1, help='You are advised to set the number of processes to the same as the number of CPU cores'
    )
    parser.add_argument(
        '--fake_processing', action='store_true', help='This is a arg to help debuggers to test progress feature and so on'
    )
    args = parser.parse_args()
    f0p = args.f0_predictor
    logger.info("Use [red]{}[/red] as speech encoder",speech_encoder)
    logger.info("Use [red]{}[/red] as f0 predictor",f0p)
    if args.use_diff:
        logger.info("[red][!][/red] Option [green]use_diff[/green] has been activated and will process shallow diffusion data for you",f0p)
    # print(f0p)
    # print(args.use_diff)

    # global fake_processing
    fake_processing = args.fake_processing

    if fake_processing: # 现在不用 logger 以后改起来得去世吧() 真的 TS 写多了 写成 __index__.py 了艹
        # 那你加先（
        # 我改这玩意也要去世了
        logger.info("Fake processing is enable")

    if args.use_diff:
        logger.info("[green][Diffusion][/green] Loading Mel Extractor...")
        mel_extractor = Vocoder(dconfig.vocoder.type, dconfig.vocoder.ckpt, device = "cuda:0")
        # print("Loaded Mel Extractor.")
        logger.success("[green][Diffusion][/green] Loaded Mel Extractor...")

    else:
        mel_extractor = None
    filenames = glob(f"{args.in_dir}/*/*.wav", recursive=True)  # [:10]
    shuffle(filenames)
    mp.set_start_method("spawn", force=True)

    num_processes = args.num_processes
    if num_processes == 0:
        num_processes = os.cpu_count()

    parallel_process(filenames, num_processes, f0p, args.use_diff, mel_extractor, fake_processing)
