import argparse
import os, sys, torch, warnings

from lib.separators import MDXNet, UVR5Base, UVR5New
from webui.audio import load_input_audio, pad_audio, remix_audio, save_input_audio
from webui.downloader import BASE_CACHE_DIR
from webui.utils import gc_collect, get_optimal_threads

CWD = os.getcwd()
if CWD not in sys.path:
    sys.path.append(CWD)
CACHED_SONGS_DIR = os.path.join(BASE_CACHE_DIR,"songs")

warnings.filterwarnings("ignore")
import numpy as np

class Separator:
    def __init__(self, model_path, use_cache=False, device="cpu", cache_dir=None, **kwargs):
        dereverb = "reverb" in model_path.lower()
        deecho = "echo"  in model_path.lower()
        denoise = dereverb or deecho

        if "MDX" in model_path:
            self.model = MDXNet(model_path=model_path,denoise=denoise,device=device,**kwargs)
        elif "UVR" in model_path:
            self.model = UVR5New(model_path=model_path,device=device,dereverb=dereverb,**kwargs) if denoise else UVR5Base(model_path=model_path,device=device,**kwargs)
            
        self.use_cache = use_cache
        self.cache_dir = cache_dir
        self.model_path = model_path
        self.args = kwargs
    
    # cleanup memory
    def __del__(self):
        gc_collect()

    def run_inference(self, audio_path, format="mp3"):
        song_name = get_filename(os.path.basename(self.model_path).split(".")[0],**self.args) + f".{format}"
        
        # handles loading of previous processed data
        music_dir = os.path.join(
            os.path.dirname(audio_path) if self.cache_dir is None else self.cache_dir,
            os.path.basename(audio_path).split(".")[0])
        vocals_path = os.path.join(music_dir,".vocals")
        instrumental_path = os.path.join(music_dir,".instrumental")
        vocals_file = os.path.join(vocals_path,song_name)
        instrumental_file = os.path.join(instrumental_path,song_name)
        os.makedirs(vocals_path,exist_ok=True)
        os.makedirs(instrumental_path,exist_ok=True)
        # input_audio = load_input_audio(audio_path,mono=True)

        if os.path.exists(instrumental_file) and os.path.exists(vocals_file):
            vocals = load_input_audio(vocals_file,mono=True)
            instrumental = load_input_audio(instrumental_file,mono=True)
            input_audio = load_input_audio(audio_path,mono=True)
            return vocals, instrumental, input_audio
        
        return_dict = self.model.run_inference(audio_path)
        instrumental = return_dict["instrumentals"]
        vocals = return_dict["vocals"]
        input_audio = return_dict["input_audio"]

        if self.use_cache:
            save_input_audio(vocals_file,vocals,to_int16=True)
            save_input_audio(instrumental_file,instrumental,to_int16=True)

        return vocals, instrumental, input_audio

def get_filename(*args,**kwargs):
    name = "_".join([str(arg) for arg in args]+[f"{k}={v}" for k,v in kwargs.items()])
    return name

def __run_inference_worker(arg):
    (model_path,audio_path,agg,device,use_cache,cache_dir,num_threads) = arg
    
    model = Separator(
            agg=agg,
            model_path=model_path,
            device=device,
            is_half="cuda" in str(device),
            use_cache=use_cache,
            cache_dir=cache_dir,
            num_threads = num_threads
            )
    vocals, instrumental, input_audio = model.run_inference(audio_path)
    del model
    gc_collect()

    return vocals, instrumental, input_audio
    
def split_audio(model_paths,audio_path,preprocess_models=[],device="cuda",agg=10,use_cache=False,merge_type="mean",**kwargs):
    print(f"unused kwargs={kwargs}")
    merge_func = np.nanmedian if merge_type=="median" else np.nanmean
    num_threads = max(get_optimal_threads(-1),1)
    song_name = os.path.basename(audio_path).split(".")[0]
    cache_dir = CACHED_SONGS_DIR

    if len(preprocess_models):
        output_name = get_filename(*[os.path.basename(name).split(".")[0] for name in preprocess_models],agg=agg) + ".mp3"
        preprocessed_file = os.path.join(cache_dir,song_name,output_name)
        
        # read from cache
        if os.path.isfile(preprocessed_file): input_audio = load_input_audio(preprocessed_file,mono=True)
        else: # preprocess audio
            for i,preprocess_model in enumerate(preprocess_models):
                output_name = get_filename(i,os.path.basename(preprocess_model).split(".")[0],agg=agg) + ".mp3"
                intermediary_file = os.path.join(cache_dir,song_name,output_name)
                if os.path.isfile(intermediary_file):
                    if i==len(preprocess_model)-1: #last model
                        input_audio = load_input_audio(intermediary_file, mono=True)
                else:
                    args = (preprocess_model,audio_path,agg,device,use_cache,CACHED_SONGS_DIR if i==0 else None,num_threads)
                    _, instrumental, input_audio = __run_inference_worker(args)
                    output_name = get_filename(i,os.path.basename(preprocess_model).split(".")[0],agg=agg) + ".mp3"
                    save_input_audio(intermediary_file,instrumental,to_int16=True)
                audio_path = intermediary_file

            save_input_audio(preprocessed_file,instrumental,to_int16=True)
        audio_path = preprocessed_file
        cache_dir = os.path.join(CACHED_SONGS_DIR,song_name)
    else:
        input_audio = load_input_audio(audio_path,mono=True)
        cache_dir = CACHED_SONGS_DIR
        
    wav_instrument = []
    wav_vocals = []

    for model_path in model_paths:
        args = (model_path,audio_path,agg,device,use_cache,cache_dir,num_threads)
        vocals, instrumental, _ = __run_inference_worker(args)
        wav_vocals.append(vocals[0])
        wav_instrument.append(instrumental[0])

    wav_instrument = merge_func(pad_audio(*wav_instrument),axis=0)
    wav_vocals = merge_func(pad_audio(*wav_vocals),axis=0)
    instrumental = remix_audio((wav_instrument,instrumental[1]),norm=True,to_int16=True,to_mono=True)
    vocals = remix_audio((wav_vocals,vocals[1]),norm=True,to_int16=True,to_mono=True)

    return vocals, instrumental, input_audio

def main(): #uvr5_models,audio_path,device="cuda",agg=10,use_cache=False
    
    parser = argparse.ArgumentParser(description="processes audio to split vocal stems and reduce reverb/echo")
    
    parser.add_argument("uvr5_models", type=str, nargs="+", help="Path to models to use for processing")
    parser.add_argument(
        "-i", "--audio_path", type=str, help="path to audio file to process", required=True
    )
    parser.add_argument(
        "-p", "--preprocess_model", type=str, help="preprocessing model to improve audio", default=None
    )
    parser.add_argument(
        "-a", "--agg", type=int, default=10, help="aggressiveness score for processing (0-20)"
    )
    parser.add_argument(
        "-d", "--device", type=str, default="cpu", choices=["cpu","cuda"], help="perform calculations on [cpu] or [cuda]"
    )
    parser.add_argument(
        "-m", "--merge_type", type=str, default="median", choices=["mean","median"], help="how to combine processed audio"
    )
    parser.add_argument(
        "-c", "--use_cache", type=bool, action="store_true", default=False, help="caches the results so next run is faster"
    )
    args = parser.parse_args()
    return split_audio(**vars(args))

if __name__ == "__main__":
    torch.multiprocessing.set_start_method("spawn")
    main()