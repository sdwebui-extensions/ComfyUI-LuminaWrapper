import torch
import os
import sys
import math
import gc

import comfy.model_management as mm
from comfy.utils import ProgressBar, load_torch_file

import folder_paths

script_directory = os.path.dirname(os.path.abspath(__file__))
sys.path.append(script_directory)

import lumina_models
from transport import ODE
from transformers import AutoModel, AutoTokenizer, GemmaForCausalLM
from argparse import Namespace

from contextlib import nullcontext
try:
    from accelerate import init_empty_weights
    from accelerate.utils import set_module_tensor_to_device
    is_accelerate_available = True
except:
    pass

try:
    from flash_attn import flash_attn_varlen_func
    FLASH_ATTN_AVAILABLE = True
    print("Flash Attention is available")
except:
    FLASH_ATTN_AVAILABLE = False
    print("LuminaWrapper: WARNING! Flash Attention is not available, using much slower torch SDP attention")

class DownloadAndLoadLuminaModel:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "model": (
                    [ 
                    'Alpha-VLLM/Lumina-Next-SFT',
                    'Alpha-VLLM/Lumina-Next-T2I'
                    ],
                    {
                    "default": 'Alpha-VLLM/Lumina-Next-SFT'
                    }),
            "precision": ([ 'bf16','fp32'],
                    {
                    "default": 'bf16'
                    }),
            },
        }

    RETURN_TYPES = ("LUMINAMODEL",)
    RETURN_NAMES = ("lumina_model",)
    FUNCTION = "loadmodel"
    CATEGORY = "LuminaWrapper"

    def loadmodel(self, model, precision):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]

        model_name = model.rsplit('/', 1)[-1]
        model_path = os.path.join(folder_paths.models_dir, "lumina", model_name)
        safetensors_path = os.path.join(model_path, "consolidated.00-of-01.safetensors")
        
        if not os.path.exists(safetensors_path):
            if os.path.exists(os.path.join("/stable-diffusion-cache/models", "lumina", model_name)):
                model_path = os.path.join("/stable-diffusion-cache/models", "lumina", model_name)
                safetensors_path = os.path.join(model_path, "consolidated.00-of-01.safetensors")
            else:
                print(f"Downloading Lumina model to: {model_path}")
                from huggingface_hub import snapshot_download
                snapshot_download(repo_id=model,
                                ignore_patterns=['*ema*', '*.pth'],
                                local_dir=model_path,
                                local_dir_use_symlinks=False)
                  
        #train_args = torch.load(os.path.join(model_path, "model_args.pth"))

        train_args = Namespace(
            model='NextDiT_2B_GQA_patch2',
            image_size=1024,
            vae='sdxl',
            precision='bf16',
            grad_precision='fp32',
            grad_clip=2.0,
            wd=0.0,
            qk_norm=True,
            model_parallel_size=1
        )

        with (init_empty_weights() if is_accelerate_available else nullcontext()):
            model = lumina_models.__dict__[train_args.model](qk_norm=train_args.qk_norm, cap_feat_dim=2048)
        model.eval().to(dtype)

        sd = load_torch_file(safetensors_path)
        if is_accelerate_available:
            for key in sd:
                set_module_tensor_to_device(model, key, dtype=dtype, device=offload_device, value=sd[key])
        else:
            model.load_state_dict(sd, strict=True)
        del sd
        mm.soft_empty_cache()
        
        lumina_model = {
            'model': model, 
            'train_args': train_args,
            'dtype': dtype
            }

        return (lumina_model,)

class DownloadAndLoadGemmaModel:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "precision": ([ 'bf16','fp32'],
                    {
                    "default": 'bf16'
                    }),
            },
        }

    RETURN_TYPES = ("GEMMAODEL",)
    RETURN_NAMES = ("gemma_model",)
    FUNCTION = "loadmodel"
    CATEGORY = "LuminaWrapper"

    def loadmodel(self, precision):
        device = mm.get_torch_device()
        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]

        gemma_path = os.path.join(folder_paths.models_dir, "LLM", "gemma-2b")
          
        if not os.path.exists(gemma_path):
            if os.path.exists("/stable-diffusion-cache/models/clip/gemma-2b"):
                gemma_path = "/stable-diffusion-cache/models/clip/gemma-2b"
            else:
                print(f"Downloading Gemma model to: {gemma_path}")
                from huggingface_hub import snapshot_download
                snapshot_download(repo_id="alpindale/gemma-2b",
                                local_dir=gemma_path,
                                ignore_patterns=['*gguf*'],
                                local_dir_use_symlinks=False)
            
        tokenizer = AutoTokenizer.from_pretrained(gemma_path)
        tokenizer.padding_side = "right"

        attn_implementation = "flash_attention_2" if FLASH_ATTN_AVAILABLE and precision != "fp32" else "sdpa"
        print(f"Gemma attention mode: {attn_implementation}")

        #model_class = AutoModel if mode == 'text_encode' else GemmaForCausalLM
        model_class = GemmaForCausalLM
        text_encoder = model_class.from_pretrained(
            gemma_path, 
            torch_dtype=dtype, 
            device_map=device, 
            attn_implementation=attn_implementation,
        ).eval()

        gemma_model = {
            'tokenizer': tokenizer,
            'text_encoder': text_encoder,
        }

        return (gemma_model,)
    
class LuminaGemmaTextEncode:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "gemma_model": ("GEMMAODEL", ),
                "latent": ("LATENT", ),
                "prompt": ("STRING", {"multiline": True, "default": "",}),
                "n_prompt": ("STRING", {"multiline": True, "default": "",}),
            },
            "optional": {
                "keep_model_loaded": ("BOOLEAN", {"default": False}),
            }
        }
    
    RETURN_TYPES = ("LUMINATEMBED",)
    RETURN_NAMES =("lumina_embeds",)
    FUNCTION = "encode"
    CATEGORY = "LuminaWrapper"

    def encode(self, gemma_model, latent, prompt, n_prompt, keep_model_loaded=False):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        mm.unload_all_models()
        mm.soft_empty_cache()

        tokenizer = gemma_model['tokenizer']
        text_encoder = gemma_model['text_encoder']
        text_encoder.to(device)

        B = latent["samples"].shape[0]
        prompts = [prompt] * B + [n_prompt] * B

        text_inputs = tokenizer(
            prompts,
            padding=True,
            pad_to_multiple_of=8,
            max_length=256,
            truncation=True,
            return_tensors="pt",
        )

        text_input_ids = text_inputs.input_ids
        prompt_masks = text_inputs.attention_mask.to(device)

        prompt_embeds = text_encoder(
            input_ids=text_input_ids.to(device),
            attention_mask=prompt_masks.to(device),
            output_hidden_states=True,
        ).hidden_states[-2]

        if not keep_model_loaded:
            print("Offloading text encoder...")
            text_encoder.to(offload_device)
            mm.soft_empty_cache()
            gc.collect()
        lumina_embeds = {
            'prompt_embeds': prompt_embeds,
            'prompt_masks': prompt_masks,
        }
        
        return (lumina_embeds,)

class LuminaTextAreaAppend:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                
                "prompt": ("STRING", {"multiline": True, "default": "",}),
                "row": ("INT", {"default": 1, "min": 1, "max": 8, "step": 1}),
                "column": ("INT", {"default": 1, "min": 1, "max": 8, "step": 1}),
            },
            "optional": {
                "prev_prompt": ("LUMINAAREAPROMPT", ),
            }
        }
    
    RETURN_TYPES = ("LUMINAAREAPROMPT",)
    RETURN_NAMES =("lumina_area_prompt",)
    FUNCTION = "process"
    CATEGORY = "LuminaWrapper"

    def process(self, prompt, row, column, prev_prompt=None):
        prompt_entry = {
            'prompt': prompt,
            'row': row,
            'column': column
        }

        if prev_prompt is not None:
            prompt_list = prev_prompt + [prompt_entry]
        else:
            prompt_list = [prompt_entry]

        return (prompt_list,)
        
class LuminaGemmaTextEncodeArea:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "gemma_model": ("GEMMAODEL", ),
                "lumina_area_prompt": ("LUMINAAREAPROMPT",),
                "append_prompt": ("STRING", {"multiline": True, "default": "",}),
                "n_prompt": ("STRING", {"multiline": True, "default": "",}),
                
            },
            "optional": {
                "keep_model_loaded": ("BOOLEAN", {"default": False}),
            }
        }
    
    RETURN_TYPES = ("LUMINATEMBED",)
    RETURN_NAMES =("lumina_embeds",)
    FUNCTION = "encode"
    CATEGORY = "LuminaWrapper"

    def encode(self, gemma_model, lumina_area_prompt, append_prompt, n_prompt, keep_model_loaded=False):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        tokenizer = gemma_model['tokenizer']
        text_encoder = gemma_model['text_encoder']
        text_encoder.to(device)

        prompt_list = [entry['prompt'] + "," + append_prompt for entry in lumina_area_prompt]
        global_prompt = " ".join(prompt_list)
        prompts = prompt_list + [n_prompt] + [global_prompt]
        print("prompts: ", prompts)

        text_inputs = tokenizer(
            prompts,
            padding=True,
            pad_to_multiple_of=8,
            max_length=256,
            truncation=True,
            return_tensors="pt",
        )

        text_input_ids = text_inputs.input_ids
        prompt_masks = text_inputs.attention_mask.to(device)

        prompt_embeds = text_encoder(
            input_ids=text_input_ids.to(device),
            attention_mask=prompt_masks.to(device),
            output_hidden_states=True,
        ).hidden_states[-2]
        if not keep_model_loaded:
            print("Offloading text encoder...")
            text_encoder.to(offload_device)
            mm.soft_empty_cache()
            gc.collect()
        lumina_embeds = {
            'prompt_embeds': prompt_embeds,
            'prompt_masks': prompt_masks,
            'lumina_area_prompt': lumina_area_prompt
        }
        
        return (lumina_embeds,)
    
class GemmaSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "gemma_model": ("GEMMAODEL", ),
                "prompt": ("STRING", {"multiline": True, "default": "",}),
                "max_length": ("INT", {"default": 128, "min": 1, "max": 512, "step": 1}),
                "temperature": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.01}),
                "do_sample": ("BOOLEAN", {"default": True}),
                "early_stopping": ("BOOLEAN", {"default": False}),
                "top_k": ("INT", {"default": 50, "min": 0, "max": 100, "step": 1}),
                "top_p": ("FLOAT", {"default": 0.95, "min": 0.0, "max": 1.0, "step": 0.01}),
                "repetition_penalty": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "length_penalty": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
            },
            "optional": {
                "keep_model_loaded": ("BOOLEAN", {"default": False}),
            }
        }
    
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES =("string",)
    FUNCTION = "process"
    CATEGORY = "LuminaWrapper"

    def process(self, gemma_model, prompt, max_length, temperature, do_sample, top_k, top_p, repetition_penalty, 
                length_penalty, early_stopping, keep_model_loaded=False):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        mm.unload_all_models()
        mm.soft_empty_cache()

        tokenizer = gemma_model['tokenizer']
        model = gemma_model['text_encoder']
        model.to(device)

        text_inputs = tokenizer(
            prompt,
            return_tensors="pt",
        )

        text_input_ids = text_inputs.input_ids.to(device)

        result = model.generate(
            text_input_ids,
            max_length=max_length,
            temperature=temperature,
            do_sample=do_sample,
            early_stopping=early_stopping,
            top_k=top_k,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            length_penalty=length_penalty,
        )
        decoded = tokenizer.batch_decode(result, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]

        print(decoded)

        if not keep_model_loaded:
            print("Offloading text encoder...")
            model.to(offload_device)
            mm.soft_empty_cache()
            gc.collect()
        
        return (decoded,)

class LuminaT2ISampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
            "lumina_model": ("LUMINAMODEL", ),
            "lumina_embeds": ("LUMINATEMBED", ),
            "latent": ("LATENT", ),
            "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            "steps": ("INT", {"default": 25, "min": 1, "max": 200, "step": 1}),
            "cfg": ("FLOAT", {"default": 4.0, "min": 0.0, "max": 20.0, "step": 0.01}),
            "proportional_attn": ("BOOLEAN", {"default": False}),
            "do_extrapolation": ("BOOLEAN", {"default": False}),
            "scaling_watershed": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 1.0, "step": 0.01}),
            "t_shift": ("INT", {"default": 4, "min": 1, "max": 20, "step": 1}),
            "solver": (
            [   
                'euler',
                'midpoint',
                'rk4',
            ],
            {
            "default": 'midpoint'
             }),
            },
            "optional": {
                "keep_model_loaded": ("BOOLEAN", {"default": False}),
                "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
            }
        }
    
    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES =("samples",)
    FUNCTION = "process"
    CATEGORY = "LuminaWrapper"

    def process(self, lumina_model, lumina_embeds, latent, seed, steps, cfg, proportional_attn, solver, t_shift, 
                do_extrapolation, scaling_watershed, strength=1.0, keep_model_loaded=False):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        model = lumina_model['model']
        dtype = lumina_model['dtype']

        vae_scaling_factor = 0.13025 #SDXL scaling factor
        
        x1 = latent["samples"].clone() * vae_scaling_factor

        ode = ODE(steps, solver, t_shift, strength)

        B = x1.shape[0]
        W = x1.shape[3] * 8
        H = x1.shape[2] * 8

        z = torch.zeros_like(x1)

        for i in range(B):
            torch.manual_seed(seed + i)
            z[i] = torch.randn_like(x1[i])
            z[i] = z[i] * (1 - ode.t[0]) + x1[i] * ode.t[0]
        
        #torch.random.manual_seed(int(seed))
        #z = torch.randn([1, 4, z.shape[2], z.shape[3]], device=device)
       
        z = z.repeat(2, 1, 1, 1)
        z = z.to(dtype).to(device)

        train_args = lumina_model['train_args']

        cap_feats=lumina_embeds['prompt_embeds']
        cap_mask=lumina_embeds['prompt_masks']

        #calculate splits from prompt dict
        if 'lumina_area_prompt' in lumina_embeds:
            unique_rows = {entry['row'] for entry in lumina_embeds['lumina_area_prompt']}
            unique_columns = {entry['column'] for entry in lumina_embeds['lumina_area_prompt']}

            horizontal_splits = len(unique_columns)
            vertical_splits = len(unique_rows)
            print(f"Horizontal splits: {horizontal_splits} Vertical splits: {vertical_splits}")
            is_split=True
        else:
            horizontal_splits = 1
            vertical_splits = 1
            is_split=False

        model_kwargs = dict(
                        cap_feats=cap_feats[:-1] if is_split else cap_feats,
                        cap_mask=cap_mask[:-1] if is_split else cap_mask,
                        global_cap_feats=cap_feats[-1:] if is_split else cap_feats,
                        global_cap_mask=cap_mask[-1:] if is_split else cap_mask,
                        cfg_scale=cfg,
                        h_split_num=int(vertical_splits),
                        w_split_num=int(horizontal_splits),
                    )
        if proportional_attn:
            model_kwargs["proportional_attn"] = True
            model_kwargs["base_seqlen"] = (train_args.image_size // 16) ** 2
        else:
            model_kwargs["proportional_attn"] = False
            model_kwargs["base_seqlen"] = None

        if do_extrapolation:
            model_kwargs["scale_factor"] = math.sqrt(W * H / train_args.image_size**2)
            model_kwargs["scale_watershed"] = scaling_watershed
        else:
            model_kwargs["scale_factor"] = 1.0
            model_kwargs["scale_watershed"] = 1.0

        def offload_model():
            print("Offloading Lumina model...")
            model.to(offload_device)
            mm.soft_empty_cache()
            gc.collect()

        #inference
        model.to(device)
        try:
            samples = ode.sample(z, model.forward_with_cfg, **model_kwargs)[-1]
        except Exception as e:
            if not keep_model_loaded:
                offload_model()
            print(e)
            raise mm.InterruptProcessingException()

        if not keep_model_loaded:
            offload_model()           
            
        samples = samples[:len(samples) // 2]
        samples = samples / vae_scaling_factor

        return ({'samples': samples},)   
     
NODE_CLASS_MAPPINGS = {
    "LuminaT2ISampler": LuminaT2ISampler,
    "DownloadAndLoadLuminaModel": DownloadAndLoadLuminaModel,
    "DownloadAndLoadGemmaModel": DownloadAndLoadGemmaModel,
    "LuminaGemmaTextEncode": LuminaGemmaTextEncode,
    "LuminaGemmaTextEncodeArea": LuminaGemmaTextEncodeArea,
    "LuminaTextAreaAppend": LuminaTextAreaAppend,
    "GemmaSampler": GemmaSampler
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "LuminaT2ISampler": "Lumina T2I Sampler",
    "DownloadAndLoadLuminaModel": "DownloadAndLoadLuminaModel",
    "DownloadAndLoadGemmaModel": "DownloadAndLoadGemmaModel",
    "LuminaGemmaTextEncode": "Lumina Gemma Text Encode",
    "LuminaGemmaTextEncodeArea": "Lumina Gemma Text Encode Area",
    "LuminaTextAreaAppend": "Lumina Text Area Append",
    "GemmaSampler": "Gemma Sampler"
}