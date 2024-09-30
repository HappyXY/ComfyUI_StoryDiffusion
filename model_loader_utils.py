# !/usr/bin/env python
# -*- coding: UTF-8 -*-
import datetime
import logging
import os
import sys
import re
import random
import copy
import torch
import torch.nn.functional as F
from omegaconf import OmegaConf
from PIL import Image
import numpy as np
import cv2
from safetensors.torch import load_file
from huggingface_hub import hf_hub_download
from transformers import CLIPImageProcessor
from diffusers import (StableDiffusionXLPipeline,  DDIMScheduler, ControlNetModel,
                       KDPM2AncestralDiscreteScheduler, LMSDiscreteScheduler,
                        DPMSolverMultistepScheduler, DPMSolverSinglestepScheduler,
                       EulerDiscreteScheduler, HeunDiscreteScheduler,
                       KDPM2DiscreteScheduler,
                       EulerAncestralDiscreteScheduler, UniPCMultistepScheduler,
                       StableDiffusionXLControlNetPipeline, DDPMScheduler, LCMScheduler)

from .msdiffusion.models.projection import Resampler
from .msdiffusion.models.model import MSAdapter
from .msdiffusion.utils import get_phrase_idx, get_eot_idx
from .utils.style_template import styles
from .ip_adapter.attention_processor import IPAttnProcessor2_0
from .utils.gradio_utils import is_torch2_available,cal_attn_indice_xl_effcient_memory,process_original_prompt,get_ref_character,character_to_dict
from .utils.load_models_utils import  get_lora_dict,get_instance_path
from .PuLID.pulid.utils import resize_numpy_image_long

if is_torch2_available():
    from .utils.gradio_utils import AttnProcessor2_0 as AttnProcessor
else:
    from .utils.gradio_utils import AttnProcessor

from comfy.utils import common_upscale
import folder_paths
from comfy.model_management import cleanup_models
from comfy.clip_vision import load as clip_load

cur_path = os.path.dirname(os.path.abspath(__file__))
photomaker_dir=os.path.join(folder_paths.models_dir, "photomaker")
base_pt = os.path.join(photomaker_dir,"pt")
device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"

lora_get = get_lora_dict()
lora_lightning_list = lora_get["lightning_xl_lora"]

global total_count, attn_count, cur_step, mask1024, mask4096, attn_procs, unet
global sa32, sa64
global write
global height_s, width_s

SAMPLER_NAMES = ["euler", "euler_cfg_pp", "euler_ancestral", "euler_ancestral_cfg_pp", "heun", "heunpp2","dpm_2", "dpm_2_ancestral",
                  "lms", "dpm_fast", "dpm_adaptive", "dpmpp_2s_ancestral", "dpmpp_2s_ancestral_cfg_pp", "dpmpp_sde", "dpmpp_sde_gpu",
                  "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu", "dpmpp_3m_sde", "dpmpp_3m_sde_gpu", "ddpm", "lcm",
                  "ipndm", "ipndm_v", "deis","ddim", "uni_pc", "uni_pc_bh2"]

SCHEDULER_NAMES = ["normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform", "beta"]


def get_scheduler(name,scheduler_):
    scheduler = False
    if name == "euler" or name =="euler_cfg_pp":
        scheduler = EulerDiscreteScheduler()
    elif name == "euler_ancestral" or name =="euler_ancestral_cfg_pp":
        scheduler = EulerAncestralDiscreteScheduler()
    elif name == "ddim":
        scheduler = DDIMScheduler()
    elif name == "ddpm":
        scheduler = DDPMScheduler()
    elif name == "dpmpp_2m":
        scheduler = DPMSolverMultistepScheduler()
    elif name == "dpmpp_2m" and scheduler_=="karras":
        scheduler = DPMSolverMultistepScheduler(use_karras_sigmas=True)
    elif name == "dpmpp_2m_sde":
        scheduler = DPMSolverMultistepScheduler(algorithm_type="sde-dpmsolver++")
    elif name == "dpmpp_2m" and scheduler_=="karras":
        scheduler = DPMSolverMultistepScheduler(use_karras_sigmas=True, algorithm_type="sde-dpmsolver++")
    elif name == "dpmpp_sde" or name == "dpmpp_sde_gpu":
        scheduler = DPMSolverSinglestepScheduler()
    elif (name == "dpmpp_sde" or name == "dpmpp_sde_gpu") and scheduler_=="karras":
        scheduler = DPMSolverSinglestepScheduler(use_karras_sigmas=True)
    elif name == "dpm_2":
        scheduler = KDPM2DiscreteScheduler()
    elif name == "dpm_2" and scheduler_=="karras":
        scheduler = KDPM2DiscreteScheduler(use_karras_sigmas=True)
    elif name == "dpm_2_ancestral":
        scheduler = KDPM2AncestralDiscreteScheduler()
    elif name == "dpm_2_ancestral" and scheduler_=="karras":
        scheduler = KDPM2AncestralDiscreteScheduler(use_karras_sigmas=True)
    elif name == "heun":
        scheduler = HeunDiscreteScheduler()
    elif name == "lcm":
        scheduler = LCMScheduler()
    elif name == "lms":
        scheduler = LMSDiscreteScheduler()
    elif name == "lms" and scheduler_=="karras":
        scheduler = LMSDiscreteScheduler(use_karras_sigmas=True)
    elif name == "uni_pc":
        scheduler = UniPCMultistepScheduler()
    else:
        scheduler = EulerDiscreteScheduler()
    return scheduler


def get_easy_function(easy_function, clip_vision, character_weights, ckpt_name, lora, repo_id,photomake_mode):
    auraface = False
    NF4 = False
    save_model = False
    kolor_face = False
    flux_pulid_name = "flux-dev"
    pulid = False
    quantized_mode = "fp16"
    story_maker = False
    make_dual_only = False
    clip_vision_path = None
    char_files = ""
    lora_path = None
    use_kolor = False
    use_flux = False
    ckpt_path = None
    onnx_provider="gpu"
    if easy_function:
        easy_function = easy_function.strip().lower()
        if "auraface" in easy_function:
            auraface = True
        if "nf4" in easy_function:
            NF4 = True
        if "save" in easy_function:
            save_model = True
        if "face" in easy_function:
            kolor_face = True
        if "schnell" in easy_function:
            flux_pulid_name = "flux-schnell"
        if "pulid" in easy_function:
            pulid = True
        if "fp8" in easy_function:
            quantized_mode = "fp8"
        if "maker" in easy_function:
            story_maker = True
        if "dual" in easy_function:
            make_dual_only = True
        if "cpu" in easy_function:
            onnx_provider="cpu"
    if clip_vision != "none":
        clip_vision_path = folder_paths.get_full_path("clip_vision", clip_vision)
    if character_weights != "none":
        character_weights_path = get_instance_path(os.path.join(base_pt, character_weights))
        weights_list = os.listdir(character_weights_path)
        if weights_list:
            char_files = character_weights_path
    if ckpt_name != "none":
        ckpt_path = folder_paths.get_full_path("checkpoints", ckpt_name)
    if lora != "none":
        lora_path = folder_paths.get_full_path("loras", lora)
        lora_path = get_instance_path(lora_path)
        if "/" in lora:
            lora = lora.split("/")[-1]
        if "\\" in lora:
            lora = lora.split("\\")[-1]
    else:
        lora = None
    if repo_id:
        if repo_id.rsplit("/")[-1].lower() in "kwai-kolors/kolors":
            use_kolor = True
            photomake_mode = ""
        elif repo_id.rsplit("/")[-1].lower() in "black-forest-labs/flux.1-dev,black-forest-labs/flux.1-schnell":
            use_flux = True
            photomake_mode = ""
        else:
            raise "no '/' in repo_id ,please change'\' to '/'"
    
    return auraface, NF4, save_model, kolor_face, flux_pulid_name, pulid, quantized_mode, story_maker, make_dual_only, clip_vision_path, char_files, ckpt_path, lora, lora_path, use_kolor, photomake_mode, use_flux,onnx_provider
def pre_checkpoint(photomaker_path, photomake_mode, kolor_face, pulid, story_maker, clip_vision_path, use_kolor,
                   model_type):
    if photomake_mode == "v1":
        if not os.path.exists(photomaker_path):
            photomaker_path = hf_hub_download(
                repo_id="TencentARC/PhotoMaker",
                filename="photomaker-v1.bin",
                local_dir=photomaker_dir,
            )
    else:
        if not os.path.exists(photomaker_path):
            photomaker_path = hf_hub_download(
                repo_id="TencentARC/PhotoMaker-V2",
                filename="photomaker-v2.bin",
                local_dir=photomaker_dir,
            )
    if kolor_face:
        face_ckpt = os.path.join(photomaker_dir, "ipa-faceid-plus.bin")
        if not os.path.exists(face_ckpt):
            hf_hub_download(
                repo_id="Kwai-Kolors/Kolors-IP-Adapter-FaceID-Plus",
                filename="ipa-faceid-plus.bin",
                local_dir=photomaker_dir,
            )
        photomake_mode = ""
    else:
        face_ckpt = ""
    if pulid:
        pulid_ckpt = os.path.join(photomaker_dir, "pulid_flux_v0.9.0.safetensors")
        if not os.path.exists(pulid_ckpt):
            hf_hub_download(
                repo_id="guozinan/PuLID",
                filename="pulid_flux_v0.9.0.safetensors",
                local_dir=photomaker_dir,
            )
        photomake_mode = ""
    else:
        pulid_ckpt = ""
    if story_maker:
        photomake_mode = ""
        if not clip_vision_path:
            raise ("using story_maker need choice a clip_vision model")
        # image_encoder_path='laion/CLIP-ViT-H-14-laion2B-s32B-b79K'
        face_adapter = os.path.join(photomaker_dir, "mask.bin")
        if not os.path.exists(face_adapter):
            hf_hub_download(
                repo_id="RED-AIGC/StoryMaker",
                filename="mask.bin",
                local_dir=photomaker_dir,
            )
    else:
        face_adapter = ""
    
    kolor_ip_path=""
    if use_kolor:
        if model_type == "img2img" and not kolor_face:
            kolor_ip_path = os.path.join(photomaker_dir, "ip_adapter_plus_general.bin")
            if not os.path.exists(kolor_ip_path):
                hf_hub_download(
                    repo_id="Kwai-Kolors/Kolors-IP-Adapter-Plus",
                    filename="ip_adapter_plus_general.bin",
                    local_dir=photomaker_dir,
                )
            photomake_mode = ""
    return photomaker_path, face_ckpt, photomake_mode, pulid_ckpt, face_adapter, kolor_ip_path


def phi2narry(img):
    img = torch.from_numpy(np.array(img).astype(np.float32) / 255.0).unsqueeze(0)
    return img

def tensor_to_image(tensor):
    image_np = tensor.squeeze().mul(255).clamp(0, 255).byte().numpy()
    image = Image.fromarray(image_np, mode='RGB')
    return image

def nomarl_tensor_upscale(tensor, width, height):
    samples = tensor.movedim(-1, 1)
    samples = common_upscale(samples, width, height, "nearest-exact", "center")
    samples = samples.movedim(1, -1)
    return samples
def nomarl_upscale(img, width, height):
    samples = img.movedim(-1, 1)
    img = common_upscale(samples, width, height, "nearest-exact", "center")
    samples = img.movedim(1, -1)
    img = tensor_to_image(samples)
    return img
def nomarl_upscale_tensor(img, width, height):
    samples = img.movedim(-1, 1)
    img = common_upscale(samples, width, height, "nearest-exact", "center")
    samples = img.movedim(1, -1)
    return samples
    
def center_crop(img):
    width, height = img.size
    square = min(width, height)
    left = (width - square) / 2
    top = (height - square) / 2
    right = (width + square) / 2
    bottom = (height + square) / 2
    return img.crop((left, top, right, bottom))

def center_crop_s(img, new_width, new_height):
    width, height = img.size
    left = (width - new_width) / 2
    top = (height - new_height) / 2
    right = (width + new_width) / 2
    bottom = (height + new_height) / 2
    return img.crop((left, top, right, bottom))


def contains_brackets(s):
    return '[' in s or ']' in s

def has_parentheses(s):
    return bool(re.search(r'\(.*?\)', s))
def extract_content_from_brackets(text):
    # 正则表达式匹配多对方括号内的内容
    return re.findall(r'\[(.*?)\]', text)

def narry_list(list_in):
    for i in range(len(list_in)):
        value = list_in[i]
        modified_value = phi2narry(value)
        list_in[i] = modified_value
    return list_in
def remove_punctuation_from_strings(lst):
    pattern = r"[\W]+$"  # 匹配字符串末尾的所有非单词字符
    return [re.sub(pattern, '', s) for s in lst]

def phi_list(list_in):
    for i in range(len(list_in)):
        value = list_in[i]
        list_in[i] = value
    return list_in

def narry_list_pil(list_in):
    for i in range(len(list_in)):
        value = list_in[i]
        modified_value = tensor_to_image(value)
        list_in[i] = modified_value
    return list_in

def get_local_path(file_path, model_path):
    path = os.path.join(file_path, "models", "diffusers", model_path)
    model_path = os.path.normpath(path)
    if sys.platform.startswith('win32'):
        model_path = model_path.replace('\\', "/")
    return model_path

def setup_seed(seed):
    torch.manual_seed(seed)
    if device == "cuda":
        torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True

def apply_style_positive(style_name: str, positive: str):
    p, n = styles.get(style_name, styles[style_name])
    #print(p, "test0", n)
    return p.replace("{prompt}", positive),n
def apply_style(style_name: str, positives: list, negative: str = ""):
    p, n = styles.get(style_name, styles[style_name])
    #print(p,"test1",n)
    return [
        p.replace("{prompt}", positive) for positive in positives
    ], n + " " + negative

def array2string(arr):
    stringtmp = ""
    for i, part in enumerate(arr):
        if i != len(arr) - 1:
            stringtmp += part + "\n"
        else:
            stringtmp += part

    return stringtmp

def find_directories(base_path):
    directories = []
    for root, dirs, files in os.walk(base_path):
        for name in dirs:
            directories.append(name)
    return directories

def load_character_files(character_files: str):
    if character_files == "":
        raise "Please set a character file!"
    character_files_arr = character_files.splitlines()
    primarytext = []
    for character_file_name in character_files_arr:
        character_file = torch.load(
            character_file_name, map_location=torch.device("cpu")
        )
        character_file.eval()
        primarytext.append(character_file["character"] + character_file["description"])
    return array2string(primarytext)

def load_single_character_weights(unet, filepath):
    """
    从指定文件中加载权重到 attention_processor 类的 id_bank 中。
    参数:
    - model: 包含 attention_processor 类实例的模型。
    - filepath: 权重文件的路径。
    """
    # 使用torch.load来读取权重
    weights_to_load = torch.load(filepath, map_location=torch.device("cpu"))
    weights_to_load.eval()
    character = weights_to_load["character"]
    description = weights_to_load["description"]
    #print(character)
    for attn_name, attn_processor in unet.attn_processors.items():
        if isinstance(attn_processor, SpatialAttnProcessor2_0):
            # 转移权重到GPU（如果GPU可用的话）并赋值给id_bank
            attn_processor.id_bank[character] = {}
            for step_key in weights_to_load[attn_name].keys():

                attn_processor.id_bank[character][step_key] = [
                    tensor.to(unet.device)
                    for tensor in weights_to_load[attn_name][step_key]
                ]
    print("successsfully,load_single_character_weights")

def load_character_files_on_running(unet, character_files: str):
    if character_files == "":
        return False
    weights_list = os.listdir(character_files)#获取路径下的权重列表
    #character_files_arr = character_files.splitlines()
    for character_file in weights_list:
        path_cur=os.path.join(character_files,character_file)
        load_single_character_weights(unet, path_cur)
    return True

def save_single_character_weights(unet, character, description, filepath):
    """
    保存 attention_processor 类中的 id_bank GPU Tensor 列表到指定文件中。
    参数:
    - model: 包含 attention_processor 类实例的模型。
    - filepath: 权重要保存到的文件路径。
    """
    weights_to_save = {}
    weights_to_save["description"] = description
    weights_to_save["character"] = character
    for attn_name, attn_processor in unet.attn_processors.items():
        if isinstance(attn_processor, SpatialAttnProcessor2_0):
            # 将每个 Tensor 转到 CPU 并转为列表，以确保它可以被序列化
            #print(attn_name, attn_processor)
            weights_to_save[attn_name] = {}
            for step_key in attn_processor.id_bank[character].keys():
                weights_to_save[attn_name][step_key] = [
                    tensor.cpu()
                    for tensor in attn_processor.id_bank[character][step_key]
                ]
    # 使用torch.save保存权重
    torch.save(weights_to_save, filepath)
    
def face_bbox_to_square(bbox):
    ## l, t, r, b to square l, t, r, b
    l,t,r,b = bbox
    cent_x = (l + r) / 2
    cent_y = (t + b) / 2
    w, h = r - l, b - t
    r = max(w, h) / 2

    l0 = cent_x - r
    r0 = cent_x + r
    t0 = cent_y - r
    b0 = cent_y + r

    return [l0, t0, r0, b0]

def save_results(unet):
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    weight_folder_name =os.path.join(base_pt,f"{timestamp}")
    #创建文件夹
    if not os.path.exists(weight_folder_name):
        os.makedirs(weight_folder_name)
    global character_dict
    for char in character_dict:
        description = character_dict[char]
        save_single_character_weights(unet,char,description,os.path.join(weight_folder_name, f'{char}.pt'))

def story_maker_loader(clip_load,clip_vision_path,dir_path,ckpt_path,face_adapter,UniPCMultistepScheduler):
    logging.info("loader story_maker processing...")
    from .StoryMaker.pipeline_sdxl_storymaker import StableDiffusionXLStoryMakerPipeline
    original_config_file = os.path.join(dir_path, 'config', 'sd_xl_base.yaml')
    add_config = os.path.join(dir_path, "local_repo")
    try:
        pipe = StableDiffusionXLStoryMakerPipeline.from_single_file(
            ckpt_path, config=add_config, original_config=original_config_file,
            torch_dtype=torch.float16)
    except:
        try:
            pipe = StableDiffusionXLStoryMakerPipeline.from_single_file(
                ckpt_path, config=add_config, original_config_file=original_config_file,
                torch_dtype=torch.float16)
        except:
            raise "load pipe error!,check you diffusers"
    pipe.cuda()
    image_encoder = clip_load(clip_vision_path)
    pipe.load_storymaker_adapter(image_encoder, face_adapter, scale=0.8, lora_scale=0.8)
    pipe.scheduler = UniPCMultistepScheduler.from_config(pipe.scheduler.config)
    return pipe

def set_attention_processor(unet, id_length, is_ipadapter=False):
    global attn_procs
    attn_procs = {}
    for name in unet.attn_processors.keys():
        cross_attention_dim = (
            None
            if name.endswith("attn1.processor")
            else unet.config.cross_attention_dim
        )
        if name.startswith("mid_block"):
            hidden_size = unet.config.block_out_channels[-1]
        elif name.startswith("up_blocks"):
            block_id = int(name[len("up_blocks.")])
            hidden_size = list(reversed(unet.config.block_out_channels))[block_id]
        elif name.startswith("down_blocks"):
            block_id = int(name[len("down_blocks.")])
            hidden_size = unet.config.block_out_channels[block_id]
        if cross_attention_dim is None:
            if name.startswith("up_blocks"):
                attn_procs[name] = SpatialAttnProcessor2_0(id_length=id_length)
            else:
                attn_procs[name] = AttnProcessor()
        else:
            if is_ipadapter:
                attn_procs[name] = IPAttnProcessor2_0(
                    hidden_size=hidden_size,
                    cross_attention_dim=cross_attention_dim,
                    scale=1,
                    num_tokens=4,
                ).to(unet.device, dtype=torch.float16)
            else:
                attn_procs[name] = AttnProcessor()

    unet.set_attn_processor(copy.deepcopy(attn_procs))

class SpatialAttnProcessor2_0(torch.nn.Module):
    r"""
    Attention processor for IP-Adapater for PyTorch 2.0.
    Args:
        hidden_size (`int`):
            The hidden size of the attention layer.
        cross_attention_dim (`int`):
            The number of channels in the `encoder_hidden_states`.
        text_context_len (`int`, defaults to 77):
            The context length of the text features.
        scale (`float`, defaults to 1.0):
            the weight scale of image prompt.
    """

    def __init__(
            self,
            hidden_size=None,
            cross_attention_dim=None,
            id_length=4,
            device=device,
            dtype=torch.float16,
    ):
        super().__init__()
        if not hasattr(F, "scaled_dot_product_attention"):
            raise ImportError(
                "AttnProcessor2_0 requires PyTorch 2.0, to use it, please upgrade PyTorch to 2.0."
            )
        self.device = device
        self.dtype = dtype
        self.hidden_size = hidden_size
        self.cross_attention_dim = cross_attention_dim
        self.total_length = id_length + 1
        self.id_length = id_length
        self.id_bank = {}

    def __call__(
            self,
            attn,
            hidden_states,
            encoder_hidden_states=None,
            attention_mask=None,
            temb=None,
    ):
        # un_cond_hidden_states, cond_hidden_states = hidden_states.chunk(2)
        # un_cond_hidden_states = self.__call2__(attn, un_cond_hidden_states,encoder_hidden_states,attention_mask,temb)
        # 生成一个0到1之间的随机数
        global total_count, attn_count, cur_step, indices1024, indices4096
        global sa32, sa64
        global write
        global height_s, width_s
        global character_dict
        global  character_index_dict, invert_character_index_dict, cur_character, ref_indexs_dict, ref_totals, cur_character
        if attn_count == 0 and cur_step == 0:
            indices1024, indices4096 = cal_attn_indice_xl_effcient_memory(
                self.total_length,
                self.id_length,
                sa32,
                sa64,
                height_s,
                width_s,
                device=self.device,
                dtype=self.dtype,
            )
        if write:
            assert len(cur_character) == 1
            if hidden_states.shape[1] == (height_s // 32) * (width_s // 32):
                indices = indices1024
            else:
                indices = indices4096
            # print(f"white:{cur_step}")
            total_batch_size, nums_token, channel = hidden_states.shape
            img_nums = total_batch_size // 2
            hidden_states = hidden_states.reshape(-1, img_nums, nums_token, channel)
            # print(img_nums,len(indices),hidden_states.shape,self.total_length)
            if cur_character[0] not in self.id_bank:
                self.id_bank[cur_character[0]] = {}
            self.id_bank[cur_character[0]][cur_step] = [
                hidden_states[:, img_ind, indices[img_ind], :]
                .reshape(2, -1, channel)
                .clone()
                for img_ind in range(img_nums)
            ]
            hidden_states = hidden_states.reshape(-1, nums_token, channel)
            # self.id_bank[cur_step] = [hidden_states[:self.id_length].clone(), hidden_states[self.id_length:].clone()]
        else:
            # encoder_hidden_states = torch.cat((self.id_bank[cur_step][0].to(self.device),self.id_bank[cur_step][1].to(self.device)))
            # TODO: ADD Multipersion Control
            encoder_arr = []
            for character in cur_character:
                encoder_arr = encoder_arr + [
                    tensor.to(self.device)
                    for tensor in self.id_bank[character][cur_step]
                ]
        # 判断随机数是否大于0.5
        if cur_step < 1:
            hidden_states = self.__call2__(
                attn, hidden_states, None, attention_mask, temb
            )
        else:  # 256 1024 4096
            random_number = random.random()
            if cur_step < 20:
                rand_num = 0.3
            else:
                rand_num = 0.1
            # print(f"hidden state shape {hidden_states.shape[1]}")
            if random_number > rand_num:
                if hidden_states.shape[1] == (height_s // 32) * (width_s // 32):
                    indices = indices1024
                else:
                    indices = indices4096
                # print("before attention",hidden_states.shape,attention_mask.shape,encoder_hidden_states.shape if encoder_hidden_states is not None else "None")
                if write:
                    total_batch_size, nums_token, channel = hidden_states.shape
                    img_nums = total_batch_size // 2
                    hidden_states = hidden_states.reshape(
                        -1, img_nums, nums_token, channel
                    )
                    encoder_arr = [
                        hidden_states[:, img_ind, indices[img_ind], :].reshape(
                            2, -1, channel
                        )
                        for img_ind in range(img_nums)
                    ]
                    for img_ind in range(img_nums):
                        # print(img_nums)
                        # assert img_nums != 1
                        img_ind_list = [i for i in range(img_nums)]
                        # print(img_ind_list,img_ind)
                        img_ind_list.remove(img_ind)
                        # print(img_ind,invert_character_index_dict[img_ind])
                        # print(character_index_dict[invert_character_index_dict[img_ind]])
                        # print(img_ind_list)
                        # print(img_ind,img_ind_list)
                        encoder_hidden_states_tmp = torch.cat(
                            [encoder_arr[img_ind] for img_ind in img_ind_list]
                            + [hidden_states[:, img_ind, :, :]],
                            dim=1,
                        )

                        hidden_states[:, img_ind, :, :] = self.__call2__(
                            attn,
                            hidden_states[:, img_ind, :, :],
                            encoder_hidden_states_tmp,
                            None,
                            temb,
                        )
                else:
                    _, nums_token, channel = hidden_states.shape
                    # img_nums = total_batch_size // 2
                    # encoder_hidden_states = encoder_hidden_states.reshape(-1,img_nums,nums_token,channel)
                    hidden_states = hidden_states.reshape(2, -1, nums_token, channel)

                    # encoder_arr = [encoder_hidden_states[:,img_ind,indices[img_ind],:].reshape(2,-1,channel) for img_ind in range(img_nums)]
                    encoder_hidden_states_tmp = torch.cat(
                        encoder_arr + [hidden_states[:, 0, :, :]], dim=1
                    )

                    hidden_states[:, 0, :, :] = self.__call2__(
                        attn,
                        hidden_states[:, 0, :, :],
                        encoder_hidden_states_tmp,
                        None,
                        temb,
                    )
                hidden_states = hidden_states.reshape(-1, nums_token, channel)
            else:
                hidden_states = self.__call2__(
                    attn, hidden_states, None, attention_mask, temb
                )
        attn_count += 1
        if attn_count == total_count:
            attn_count = 0
            cur_step += 1
            indices1024, indices4096 = cal_attn_indice_xl_effcient_memory(
                self.total_length,
                self.id_length,
                sa32,
                sa64,
                height_s,
                width_s,
                device=self.device,
                dtype=self.dtype,
            )

        return hidden_states

    def __call2__(
            self,
            attn,
            hidden_states,
            encoder_hidden_states=None,
            attention_mask=None,
            temb=None,
    ):
        residual = hidden_states

        if attn.spatial_norm is not None:
            hidden_states = attn.spatial_norm(hidden_states, temb)

        input_ndim = hidden_states.ndim

        if input_ndim == 4:
            batch_size, channel, height_s, width_s = hidden_states.shape
            hidden_states = hidden_states.view(
                batch_size, channel, height_s * width_s
            ).transpose(1, 2)

        batch_size, sequence_length, channel = hidden_states.shape

        if attention_mask is not None:
            attention_mask = attn.prepare_attention_mask(
                attention_mask, sequence_length, batch_size
            )
            # scaled_dot_product_attention expects attention_mask shape to be
            # (batch, heads, source_length, target_length)
            attention_mask = attention_mask.view(
                batch_size, attn.heads, -1, attention_mask.shape[-1]
            )

        if attn.group_norm is not None:
            hidden_states = attn.group_norm(hidden_states.transpose(1, 2)).transpose(
                1, 2
            )

        query = attn.to_q(hidden_states)

        if encoder_hidden_states is None:
            encoder_hidden_states = hidden_states  # B, N, C
        # else:
        #     encoder_hidden_states = encoder_hidden_states.view(-1,self.id_length+1,sequence_length,channel).reshape(-1,(self.id_length+1) * sequence_length,channel)

        key = attn.to_k(encoder_hidden_states)
        value = attn.to_v(encoder_hidden_states)

        inner_dim = key.shape[-1]
        head_dim = inner_dim // attn.heads

        query = query.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)

        key = key.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)
        value = value.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)

        # the output of sdp = (batch, num_heads, seq_len, head_dim)
        # TODO: add support for attn.scale when we move to Torch 2.1
        hidden_states = F.scaled_dot_product_attention(
            query, key, value, attn_mask=attention_mask, dropout_p=0.0, is_causal=False
        )

        hidden_states = hidden_states.transpose(1, 2).reshape(
            batch_size, -1, attn.heads * head_dim
        )
        hidden_states = hidden_states.to(query.dtype)

        # linear proj
        hidden_states = attn.to_out[0](hidden_states)
        # dropout
        hidden_states = attn.to_out[1](hidden_states)

        if input_ndim == 4:
            hidden_states = hidden_states.transpose(-1, -2).reshape(
                batch_size, channel, height_s, width_s
            )

        if attn.residual_connection:
            hidden_states = hidden_states + residual

        hidden_states = hidden_states / attn.rescale_output_factor

        return hidden_states

def kolor_loader(repo_id,model_type,set_attention_processor,id_length,kolor_face,clip_vision_path,clip_load,CLIPVisionModelWithProjection,CLIPImageProcessor,
                 photomaker_dir,face_ckpt,AutoencoderKL,EulerDiscreteScheduler,UNet2DConditionModel):
    from .kolors.pipelines.pipeline_stable_diffusion_xl_chatglm_256 import \
        StableDiffusionXLPipeline as StableDiffusionXLPipelineKolors
    from .kolors.models.modeling_chatglm import ChatGLMModel
    from .kolors.models.tokenization_chatglm import ChatGLMTokenizer
    from .kolors.models.unet_2d_condition import UNet2DConditionModel as UNet2DConditionModelkolor
    logging.info("loader story_maker processing...")
    text_encoder = ChatGLMModel.from_pretrained(
        f'{repo_id}/text_encoder', torch_dtype=torch.float16).half()
    vae = AutoencoderKL.from_pretrained(f"{repo_id}/vae", revision=None).half()
    tokenizer = ChatGLMTokenizer.from_pretrained(f'{repo_id}/text_encoder')
    scheduler = EulerDiscreteScheduler.from_pretrained(f"{repo_id}/scheduler")
    if model_type == "txt2img":
        unet = UNet2DConditionModel.from_pretrained(f"{repo_id}/unet", revision=None,
                                                    use_safetensors=True).half()
        pipe = StableDiffusionXLPipelineKolors(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            scheduler=scheduler,
            force_zeros_for_empty_prompt=False, )
        set_attention_processor(pipe.unet, id_length, is_ipadapter=False)
    else:
        if kolor_face is False:
            from .kolors.pipelines.pipeline_stable_diffusion_xl_chatglm_256_ipadapter import \
                StableDiffusionXLPipeline as StableDiffusionXLPipelinekoloripadapter
            if clip_vision_path:
                image_encoder = clip_load(clip_vision_path).model
                ip_img_size = 224  # comfyUI defualt is use 224
                use_singel_clip = True
            else:
                image_encoder = CLIPVisionModelWithProjection.from_pretrained(
                    f'{repo_id}/Kolors-IP-Adapter-Plus/image_encoder', ignore_mismatched_sizes=True).to(
                    dtype=torch.float16)
                ip_img_size = 336
                use_singel_clip = False
            clip_image_processor = CLIPImageProcessor(size=ip_img_size, crop_size=ip_img_size)
            unet = UNet2DConditionModelkolor.from_pretrained(f"{repo_id}/unet", revision=None, ).half()
            pipe = StableDiffusionXLPipelinekoloripadapter(
                vae=vae,
                text_encoder=text_encoder,
                tokenizer=tokenizer,
                unet=unet,
                scheduler=scheduler,
                image_encoder=image_encoder,
                feature_extractor=clip_image_processor,
                force_zeros_for_empty_prompt=False,
                use_single_clip=use_singel_clip
            )
            if hasattr(pipe.unet, 'encoder_hid_proj'):
                pipe.unet.text_encoder_hid_proj = pipe.unet.encoder_hid_proj
            pipe.load_ip_adapter(photomaker_dir, subfolder="", weight_name=["ip_adapter_plus_general.bin"])
        else:  # kolor ip faceid
            from .kolors.pipelines.pipeline_stable_diffusion_xl_chatglm_256_ipadapter_FaceID import \
                StableDiffusionXLPipeline as StableDiffusionXLPipelineFaceID
            unet = UNet2DConditionModel.from_pretrained(f'{repo_id}/unet', revision=None).half()
            
            if clip_vision_path:
                clip_image_encoder = clip_load(clip_vision_path).model
                clip_image_processor = CLIPImageProcessor(size=224, crop_size=224)
                use_singel_clip = True
            else:
                clip_image_encoder = CLIPVisionModelWithProjection.from_pretrained(
                    f'{repo_id}/clip-vit-large-patch14-336', ignore_mismatched_sizes=True)
                clip_image_encoder.to("cuda")
                clip_image_processor = CLIPImageProcessor(size=336, crop_size=336)
                use_singel_clip = False
            
            pipe = StableDiffusionXLPipelineFaceID(
                vae=vae,
                text_encoder=text_encoder,
                tokenizer=tokenizer,
                unet=unet,
                scheduler=scheduler,
                face_clip_encoder=clip_image_encoder,
                face_clip_processor=clip_image_processor,
                force_zeros_for_empty_prompt=False,
                use_single_clip=use_singel_clip,
            )
            pipe = pipe.to("cuda")
            pipe.load_ip_adapter_faceid_plus(face_ckpt, device="cuda")
            pipe.set_face_fidelity_scale(0.8)
        return pipe
    
def flux_loader(folder_paths,ckpt_path,repo_id,AutoencoderKL,save_model,model_type,pulid,clip_vision_path,NF4,vae_id,offload,aggressive_offload,pulid_ckpt,quantized_mode,
                if_repo,dir_path,clip,onnx_provider):
    # pip install optimum-quanto
    # https://gist.github.com/AmericanPresidentJimmyCarter/873985638e1f3541ba8b00137e7dacd9
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    weight_transformer = os.path.join(folder_paths.models_dir, "checkpoints", f"transformer_{timestamp}.pt")
    dtype = torch.bfloat16
    if not ckpt_path:
        logging.info("using repo_id ,start flux fp8 quantize processing...")
        from optimum.quanto import freeze, qfloat8, quantize
        from diffusers.pipelines.flux.pipeline_flux import FluxPipeline
        from diffusers import FlowMatchEulerDiscreteScheduler
        from diffusers.models.transformers.transformer_flux import FluxTransformer2DModel
        from transformers import CLIPTextModel, CLIPTokenizer, T5EncoderModel, T5TokenizerFast
        revision = "refs/pr/1"
        scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(repo_id, subfolder="scheduler",
                                                                    revision=revision)
        text_encoder = CLIPTextModel.from_pretrained("openai/clip-vit-large-patch14", torch_dtype=dtype)
        tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14", torch_dtype=dtype)
        text_encoder_2 = T5EncoderModel.from_pretrained(repo_id, subfolder="text_encoder_2",
                                                        torch_dtype=dtype,
                                                        revision=revision)
        tokenizer_2 = T5TokenizerFast.from_pretrained(repo_id, subfolder="tokenizer_2",
                                                      torch_dtype=dtype,
                                                      revision=revision)
        vae = AutoencoderKL.from_pretrained(repo_id, subfolder="vae", torch_dtype=dtype,
                                            revision=revision)
        transformer = FluxTransformer2DModel.from_pretrained(repo_id, subfolder="transformer",
                                                             torch_dtype=dtype, revision=revision)
        quantize(transformer, weights=qfloat8)
        freeze(transformer)
        if save_model:
            print(f"saving fp8 pt on '{weight_transformer}'")
            torch.save(transformer,
                       weight_transformer)  # https://pytorch.org/tutorials/beginner/saving_loading_models.html.
        quantize(text_encoder_2, weights=qfloat8)
        freeze(text_encoder_2)
        if model_type == "img2img":
            # https://github.com/deforum-studio/flux/blob/main/flux_pipeline.py#L536
            from .utils.flux_pipeline import FluxImg2ImgPipeline
            pipe = FluxImg2ImgPipeline(
                scheduler=scheduler,
                text_encoder=text_encoder,
                tokenizer=tokenizer,
                text_encoder_2=None,
                tokenizer_2=tokenizer_2,
                vae=vae,
                transformer=None,
            )
        else:
            pipe = FluxPipeline(
                scheduler=scheduler,
                text_encoder=text_encoder,
                tokenizer=tokenizer,
                text_encoder_2=None,
                tokenizer_2=tokenizer_2,
                vae=vae,
                transformer=None,
            )
        pipe.text_encoder_2 = text_encoder_2
        pipe.transformer = transformer
        pipe.enable_model_cpu_offload()
    else:  # flux diff unet ,diff 0.30 ckpt or repo
        from diffusers import FluxTransformer2DModel, FluxPipeline
        from transformers import T5EncoderModel, CLIPTextModel
        from optimum.quanto import freeze, qfloat8, quantize
        if pulid:
            logging.info("using repo_id and ckpt ,start flux-pulid processing...")
            from .PuLID.app_flux import FluxGenerator
            if not clip_vision_path:
                raise "need 'EVA02_CLIP_L_336_psz14_s6B.pt' in comfyUI/models/clip_vision"
            if NF4:
                quantized_mode = "nf4"
            if vae_id == "none":
                raise "Now,using pulid must choice ae from comfyUI vae menu"
            else:
                vae_path = folder_paths.get_full_path("vae", vae_id)
            pipe = FluxGenerator(repo_id, ckpt_path, "cuda", offload=offload,
                                 aggressive_offload=aggressive_offload, pretrained_model=pulid_ckpt,
                                 quantized_mode=quantized_mode, clip_vision_path=clip_vision_path, clip_cf=clip,
                                 vae_cf=vae_path, if_repo=if_repo,onnx_provider=onnx_provider)
        else:
            if NF4:
                logging.info("using repo_id and ckpt ,start flux nf4 quantize processing...")
                # https://github.com/huggingface/diffusers/issues/9165
                from accelerate.utils import set_module_tensor_to_device
                from accelerate import init_empty_weights
                from .utils.convert_nf4_flux import _replace_with_bnb_linear, create_quantized_param, \
                    check_quantized_param
                import gc
                dtype = torch.bfloat16
                is_torch_e4m3fn_available = hasattr(torch, "float8_e4m3fn")
                original_state_dict = load_file(ckpt_path)
                
                config_file = os.path.join(dir_path, "config.json")
                with init_empty_weights():
                    config = FluxTransformer2DModel.load_config(config_file)
                    model = FluxTransformer2DModel.from_config(config).to(dtype)
                    expected_state_dict_keys = list(model.state_dict().keys())
                
                _replace_with_bnb_linear(model, "nf4")
                
                for param_name, param in original_state_dict.items():
                    if param_name not in expected_state_dict_keys:
                        continue
                    
                    is_param_float8_e4m3fn = is_torch_e4m3fn_available and param.dtype == torch.float8_e4m3fn
                    if torch.is_floating_point(param) and not is_param_float8_e4m3fn:
                        param = param.to(dtype)
                    
                    if not check_quantized_param(model, param_name):
                        set_module_tensor_to_device(model, param_name, device=0, value=param)
                    else:
                        create_quantized_param(
                            model, param, param_name, target_device=0, state_dict=original_state_dict,
                            pre_quantized=True
                        )
                
                del original_state_dict
                gc.collect()
                if model_type == "img2img":
                    from .utils.flux_pipeline import FluxImg2ImgPipeline
                    pipe = FluxImg2ImgPipeline.from_pretrained(repo_id, transformer=model,
                                                               torch_dtype=dtype)
                else:
                    pipe = FluxPipeline.from_pretrained(repo_id, transformer=model, torch_dtype=dtype)
            else:
                logging.info("using repo_id and ckpt ,start flux fp8 quantize processing...")
                if os.path.splitext(ckpt_path)[-1] == ".pt":
                    transformer = torch.load(ckpt_path)
                    transformer.eval()
                else:
                    config_file = os.path.join(dir_path, "utils", "config.json")
                    transformer = FluxTransformer2DModel.from_single_file(ckpt_path, config=config_file,
                                                                          torch_dtype=dtype)
                text_encoder_2 = T5EncoderModel.from_pretrained(repo_id, subfolder="text_encoder_2",
                                                                torch_dtype=dtype)
                quantize(text_encoder_2, weights=qfloat8)
                freeze(text_encoder_2)
                
                if model_type == "img2img":
                    from .utils.flux_pipeline import FluxImg2ImgPipeline
                    pipe = FluxImg2ImgPipeline.from_pretrained(repo_id, transformer=None,
                                                               text_encoder_2=clip,
                                                               torch_dtype=dtype)
                else:
                    pipe = FluxPipeline.from_pretrained(repo_id,transformer=None,text_encoder_2=None,
                                                        torch_dtype=dtype)
                pipe.transformer = transformer
                pipe.text_encoder_2 = text_encoder_2
            pipe.enable_model_cpu_offload()
    return pipe

def insight_face_loader(photomake_mode,auraface,kolor_face,story_maker,make_dual_only,photomake_mode_):
    if photomake_mode == "v2":
        from .utils.insightface_package import FaceAnalysis2, analyze_faces
        if auraface:
            from huggingface_hub import snapshot_download
            snapshot_download(
                "fal/AuraFace-v1",
                local_dir="models/auraface",
            )
            app_face = FaceAnalysis2(name="auraface",
                                     providers=["CUDAExecutionProvider", "CPUExecutionProvider"], root=".",
                                     allowed_modules=['detection', 'recognition'])
        else:
            app_face = FaceAnalysis2(providers=['CUDAExecutionProvider'],
                                     allowed_modules=['detection', 'recognition'])
        app_face.prepare(ctx_id=0, det_size=(640, 640))
        pipeline_mask = None
        app_face_ = None
    elif kolor_face:
        from .kolors.models.sample_ipadapter_faceid_plus import FaceInfoGenerator
        from huggingface_hub import snapshot_download
        snapshot_download(
            'DIAMONIK7777/antelopev2',
            local_dir='models/antelopev2',
        )
        app_face = FaceInfoGenerator(root_dir=".")
        pipeline_mask = None
        app_face_ = None
    elif story_maker:
        from insightface.app import FaceAnalysis
        from transformers import pipeline
        pipeline_mask = pipeline("image-segmentation", model="briaai/RMBG-1.4",
                                 trust_remote_code=True)
        if make_dual_only:  # 前段用story 双人用maker
            if photomake_mode_ == "v2":
                from .utils.insightface_package import FaceAnalysis2
                if auraface:
                    from huggingface_hub import snapshot_download
                    snapshot_download(
                        "fal/AuraFace-v1",
                        local_dir="models/auraface",
                    )
                    app_face = FaceAnalysis2(name="auraface",
                                             providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
                                             root=".",
                                             allowed_modules=['detection', 'recognition'])
                else:
                    app_face = FaceAnalysis2(providers=['CUDAExecutionProvider'],
                                             allowed_modules=['detection', 'recognition'])
                app_face.prepare(ctx_id=0, det_size=(640, 640))
                app_face_ = FaceAnalysis(name='buffalo_l', root='./',
                                         providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])
                app_face_.prepare(ctx_id=0, det_size=(640, 640))
            else:
                app_face = FaceAnalysis(name='buffalo_l', root='./',
                                        providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])
                app_face.prepare(ctx_id=0, det_size=(640, 640))
                app_face_ = None
        else:
            app_face = FaceAnalysis(name='buffalo_l', root='./',
                                    providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])
            app_face.prepare(ctx_id=0, det_size=(640, 640))
            app_face_ = None
    else:
        app_face = None
        pipeline_mask = None
        app_face_ = None
    return app_face,pipeline_mask,app_face_

def main_normal(prompt,pipe,phrases,ms_model,input_images,num_samples,steps,seed,negative_prompt,scale,image_encoder,cfg,image_processor,
                boxes,mask_threshold,start_step,image_proj_type,image_encoder_type,drop_grounding_tokens,height,width,phrase_idxes, eot_idxes,in_img,use_repo):
    if use_repo:
        in_img = None
    images = ms_model.generate(pipe=pipe, pil_images=[input_images],processed_images=in_img, num_samples=num_samples,
                               num_inference_steps=steps,
                               seed=seed,
                               prompt=[prompt], negative_prompt=negative_prompt, scale=scale,
                               image_encoder=image_encoder, guidance_scale=cfg,
                               image_processor=image_processor, boxes=boxes,
                               mask_threshold=mask_threshold,
                               start_step=start_step,
                               image_proj_type=image_proj_type,
                               image_encoder_type=image_encoder_type,
                               phrases=phrases,
                               drop_grounding_tokens=drop_grounding_tokens,
                               phrase_idxes=phrase_idxes, eot_idxes=eot_idxes, height=height,
                               width=width)
    return images
def main_control(prompt,width,height,pipe,phrases,ms_model,input_images,num_samples,steps,seed,negative_prompt,scale,image_encoder,cfg,
                 image_processor,boxes,mask_threshold,start_step,image_proj_type,image_encoder_type,drop_grounding_tokens,controlnet_scale,control_image,phrase_idxes, eot_idxes,in_img,use_repo):
    if use_repo:
        in_img=None
    images = ms_model.generate(pipe=pipe, pil_images=[input_images],processed_images=in_img, num_samples=num_samples,
                               num_inference_steps=steps,
                               seed=seed,
                               prompt=[prompt], negative_prompt=negative_prompt, scale=scale,
                               image_encoder=image_encoder, guidance_scale=cfg,
                               image_processor=image_processor, boxes=boxes,
                               mask_threshold=mask_threshold,
                               start_step=start_step,
                               image_proj_type=image_proj_type,
                               image_encoder_type=image_encoder_type,
                               phrases=phrases,
                               drop_grounding_tokens=drop_grounding_tokens,
                               phrase_idxes=phrase_idxes, eot_idxes=eot_idxes, height=height,
                               width=width,
                               image=control_image, controlnet_conditioning_scale=controlnet_scale)

    return images

def get_float(str_in):
    list_str=str_in.split(",")
    float_box=[float(x) for x in list_str]
    return float_box
def get_phrases_idx(tokenizer, phrases, prompt):
    res = []
    phrase_cnt = {}
    for phrase in phrases:
        if phrase in phrase_cnt:
            cur_cnt = phrase_cnt[phrase]
            phrase_cnt[phrase] += 1
        else:
            cur_cnt = 0
            phrase_cnt[phrase] = 1
        res.append(get_phrase_idx(tokenizer, phrase, prompt, num=cur_cnt)[0])
    return res

def msdiffusion_main(image_1, image_2, prompts_dual, width, height, steps, seed, style_name, char_describe, char_origin,
                     negative_prompt,
                     clip_vision, _model_type, lora, lora_path, lora_scale, trigger_words, ckpt_path, dif_repo,
                     guidance, mask_threshold, start_step, controlnet_path, control_image, controlnet_scale, cfg,
                     guidance_list, scheduler_choice):
    tensor_a = phi2narry(image_1.copy())
    tensor_b = phi2narry(image_2.copy())
    in_img = torch.cat((tensor_a, tensor_b), dim=0)
    
    original_config_file = os.path.join(cur_path, 'config', 'sd_xl_base.yaml')
    if dif_repo:
        single_files = False
    elif not dif_repo and ckpt_path:
        single_files = True
    elif dif_repo and ckpt_path:
        single_files = False
    else:
        raise "no model"
    add_config = os.path.join(cur_path, "local_repo")
    if single_files:
        try:
            pipe = StableDiffusionXLPipeline.from_single_file(
                ckpt_path, config=add_config, original_config=original_config_file,
                torch_dtype=torch.float16)
        except:
            try:
                pipe = StableDiffusionXLPipeline.from_single_file(
                    ckpt_path, config=add_config, original_config_file=original_config_file,
                    torch_dtype=torch.float16)
            except:
                raise "load pipe error!,check you diffusers"
    else:
        pipe = StableDiffusionXLPipeline.from_pretrained(dif_repo, torch_dtype=torch.float16)
    
    if controlnet_path:
        controlnet = ControlNetModel.from_unet(pipe.unet)
        cn_state_dict = load_file(controlnet_path, device="cpu")
        controlnet.load_state_dict(cn_state_dict, strict=False)
        controlnet.to(torch.float16)
        pipe = StableDiffusionXLControlNetPipeline.from_pipe(pipe, controlnet=controlnet)
    
    if lora:
        if lora in lora_lightning_list:
            pipe.load_lora_weights(lora_path)
            pipe.fuse_lora()
        else:
            pipe.load_lora_weights(lora_path, adapter_name=trigger_words)
            pipe.fuse_lora(adapter_names=[trigger_words, ], lora_scale=lora_scale)
    pipe.scheduler = scheduler_choice.from_config(pipe.scheduler.config)
    pipe.enable_xformers_memory_efficient_attention()
    pipe.enable_freeu(s1=0.6, s2=0.4, b1=1.1, b2=1.2)
    pipe.enable_vae_slicing()
    
    if device != "mps":
        pipe.enable_model_cpu_offload()
    torch.cuda.empty_cache()
    # 预加载 ms
    photomaker_local_path = os.path.join(photomaker_dir, "ms_adapter.bin")
    if not os.path.exists(photomaker_local_path):
        ms_path = hf_hub_download(
            repo_id="doge1516/MS-Diffusion",
            filename="ms_adapter.bin",
            repo_type="model",
            local_dir=photomaker_dir,
        )
    else:
        ms_path = photomaker_local_path
    ms_ckpt = get_instance_path(ms_path)
    image_processor = CLIPImageProcessor()
    image_encoder_type = "clip"
    cleanup_models(keep_clone_weights_loaded=False)
    image_encoder = clip_load(clip_vision)
    use_repo = False
    config_path = os.path.join(cur_path, "config", "config.json")
    image_encoder_config = OmegaConf.load(config_path)
    image_encoder_projection_dim = image_encoder_config["vision_config"]["projection_dim"]
    num_tokens = 16
    image_proj_type = "resampler"
    latent_init_mode = "grounding"
    # latent_init_mode = "random"
    image_proj_model = Resampler(
        dim=1280,
        depth=4,
        dim_head=64,
        heads=20,
        num_queries=num_tokens,
        embedding_dim=image_encoder_config["vision_config"]["hidden_size"],
        output_dim=pipe.unet.config.cross_attention_dim,
        ff_mult=4,
        latent_init_mode=latent_init_mode,
        phrase_embeddings_dim=pipe.text_encoder.config.projection_dim,
    ).to(device, dtype=torch.float16)
    ms_model = MSAdapter(pipe.unet, image_proj_model, ckpt_path=ms_ckpt, device=device, num_tokens=num_tokens)
    ms_model.to(device, dtype=torch.float16)
    torch.cuda.empty_cache()
    input_images = [image_1, image_2]
    batch_size = 1
    guidance_list = guidance_list.strip().split(";")
    box_add = []  # 获取预设box
    for i in range(len(guidance_list)):
        box_add.append(get_float(guidance_list[i]))
    
    if mask_threshold == 0.:
        mask_threshold = None
    
    image_ouput = []
    
    # get role name
    role_a = char_origin[0].replace("]", "").replace("[", "")
    role_b = char_origin[1].replace("]", "").replace("[", "")
    # get n p prompt
    prompts_dual, negative_prompt = apply_style(
        style_name, prompts_dual, negative_prompt
    )
    
    # 添加Lora trigger
    add_trigger_words = " " + trigger_words + " style "
    if lora:
        prompts_dual = remove_punctuation_from_strings(prompts_dual)
        if lora not in lora_lightning_list:  # 加速lora不需要trigger
            prompts_dual = [item + add_trigger_words for item in prompts_dual]
    
    prompts_dual = [item.replace(char_origin[0], char_describe[0]) for item in prompts_dual if char_origin[0] in item]
    prompts_dual = [item.replace(char_origin[1], char_describe[1]) for item in prompts_dual if char_origin[1] in item]
    
    prompts_dual = [item.replace("[", " ", ).replace("]", " ", ) for item in prompts_dual]
    # print(prompts_dual)
    torch.cuda.empty_cache()
    
    phrases = [[role_a, role_b]]
    drop_grounding_tokens = [0]  # set to 1 if you want to drop the grounding tokens
    
    if mask_threshold:
        boxes = [box_add[:2]]
        print(f"Roles position on {boxes}")
        # boxes = [[[0., 0.25, 0.4, 0.75], [0.6, 0.25, 1., 0.75]]]  # man+women
    else:
        zero_list = [0 for _ in range(4)]
        boxes = [zero_list for _ in range(2)]
        boxes = [boxes]  # used if you want no layout guidance
        # print(boxes)
        
    role_scale=guidance if guidance<=1 else guidance/10 if 1<guidance<=10 else guidance/100
    
    if controlnet_path:
        d1, _, _, _ = control_image.size()
        if d1 == 1:
            control_img_list = [control_image]
        else:
            control_img_list = torch.chunk(control_image, chunks=d1)
        j = 0
        for i, prompt in enumerate(prompts_dual):
            control_image = control_img_list[j]
            control_image = nomarl_upscale(control_image, width, height)
            j += 1
            # used to get the attention map, return zero if the phrase is not in the prompt
            phrase_idxes = [get_phrases_idx(pipe.tokenizer, phrases[0], prompt)]
            eot_idxes = [[get_eot_idx(pipe.tokenizer, prompt)] * len(phrases[0])]
            # print(phrase_idxes, eot_idxes)
            image_main = main_control(prompt, width, height, pipe, phrases, ms_model, input_images, batch_size,
                                      steps,
                                      seed, negative_prompt, role_scale, image_encoder, cfg,
                                      image_processor, boxes, mask_threshold, start_step, image_proj_type,
                                      image_encoder_type, drop_grounding_tokens, controlnet_scale, control_image,
                                      phrase_idxes, eot_idxes, in_img, use_repo)
            
            image_ouput.append(image_main)
            torch.cuda.empty_cache()
    else:
        for i, prompt in enumerate(prompts_dual):
            # used to get the attention map, return zero if the phrase is not in the prompt
            phrase_idxes = [get_phrases_idx(pipe.tokenizer, phrases[0], prompt)]
            eot_idxes = [[get_eot_idx(pipe.tokenizer, prompt)] * len(phrases[0])]
            # print(phrase_idxes, eot_idxes)
            image_main = main_normal(prompt, pipe, phrases, ms_model, input_images, batch_size, steps, seed,
                                     negative_prompt, role_scale, image_encoder, cfg, image_processor,
                                     boxes, mask_threshold, start_step, image_proj_type, image_encoder_type,
                                     drop_grounding_tokens, height, width, phrase_idxes, eot_idxes, in_img, use_repo)
            image_ouput.append(image_main)
            torch.cuda.empty_cache()
    torch.cuda.empty_cache()
    return image_ouput


def get_insight_dict(app_face,pipeline_mask,app_face_,image_load,photomake_mode,kolor_face,story_maker,make_dual_only,photomake_mode_,
                     pulid,pipe,character_list_,control_image,width, height):
    input_id_emb_s_dict = {}
    input_id_img_s_dict = {}
    input_id_emb_un_dict = {}
    for ind, img in enumerate(image_load):
        if photomake_mode == "v2":
            from .utils.insightface_package import analyze_faces
            img = np.array(img)
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            faces = analyze_faces(app_face, img, )
            id_embed_list = torch.from_numpy((faces[0]['embedding']))
            crop_image = img
            uncond_id_embeddings = None
        elif kolor_face:
            device = (
                "cuda"
                if torch.cuda.is_available()
                else "mps" if torch.backends.mps.is_available() else "cpu"
            )
            face_info = app_face.get_faceinfo_one_img(img)
            face_bbox_square = face_bbox_to_square(face_info["bbox"])
            crop_image = img.crop(face_bbox_square)
            crop_image = crop_image.resize((336, 336))
            face_embeds = torch.from_numpy(np.array([face_info["embedding"]]))
            id_embed_list = face_embeds.to(device, dtype=torch.float16)
            uncond_id_embeddings = None
        elif story_maker:
            if make_dual_only:  # 前段用story 双人用maker
                if photomake_mode_ == "v2":
                    from .utils.insightface_package import analyze_faces
                    img = np.array(img)
                    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                    faces = analyze_faces(app_face, img, )
                    id_embed_list = torch.from_numpy((faces[0]['embedding']))
                    crop_image = pipeline_mask(img, return_mask=True).convert(
                        'RGB')  # outputs a pillow mask
                    face_info = app_face_.get(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR))
                    uncond_id_embeddings = \
                        sorted(face_info,
                               key=lambda x: (x['bbox'][2] - x['bbox'][0]) * (x['bbox'][3] - x['bbox'][1]))[
                            -1]  # only use the maximum face
                    photomake_mode = "v2"
                    miX_mode = True
                    # make+v2模式下，emb存v2的向量，corp 和 unemb 存make的向量
                else:  # V1不需要调用emb
                    crop_image = pipeline_mask(img, return_mask=True).convert(
                        'RGB')  # outputs a pillow mask
                    face_info = app_face.get(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR))
                    id_embed_list = \
                        sorted(face_info,
                               key=lambda x: (x['bbox'][2] - x['bbox'][0]) * (x['bbox'][3] - x['bbox'][1]))[
                            -1]  # only use the maximum face
                    uncond_id_embeddings = None
            else:  # 全程用maker
                crop_image = pipeline_mask(img, return_mask=True).convert('RGB')  # outputs a pillow mask
                # timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                # crop_image.copy().save(os.path.join(folder_paths.get_output_directory(),f"{timestamp}_mask.png"))
                face_info = app_face.get(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR))
                id_embed_list = \
                    sorted(face_info,
                           key=lambda x: (x['bbox'][2] - x['bbox'][0]) * (x['bbox'][3] - x['bbox'][1]))[
                        -1]  # only use the maximum face
                
                uncond_id_embeddings = None
        elif pulid:
            id_image = resize_numpy_image_long(img, 1024)
            use_true_cfg = abs(1.0 - 1.0) > 1e-2
            id_embed_list, uncond_id_embeddings = pipe.pulid_model.get_id_embedding(id_image,
                                                                                    cal_uncond=use_true_cfg)
            crop_image = img
        else:
            id_embed_list = None
            uncond_id_embeddings = None
            crop_image = None
        input_id_img_s_dict[character_list_[ind]] = [crop_image]
        input_id_emb_s_dict[character_list_[ind]] = [id_embed_list]
        input_id_emb_un_dict[character_list_[ind]] = [uncond_id_embeddings]
    
    if story_maker or kolor_face or photomake_mode == "v2":
        del app_face
        torch.cuda.empty_cache()
    if story_maker:
        del pipeline_mask
        torch.cuda.empty_cache()
    
    if isinstance(control_image, torch.Tensor) and story_maker:
        e1, _, _, _ = control_image.size()
        if e1 == 1:
            cn_image_load = [nomarl_upscale(control_image, width, height)]
        else:
            img_list = list(torch.chunk(control_image, chunks=e1))
            cn_image_load = [nomarl_upscale(img, width, height) for img in img_list]
        input_id_cloth_dict = {}
        for ind, img in enumerate(cn_image_load):
            input_id_cloth_dict[character_list_[ind]] = [img]
    else:
        input_id_cloth_dict = {}
    return input_id_emb_s_dict,input_id_img_s_dict,input_id_emb_un_dict,input_id_cloth_dict
   
def process_generation(
        pipe,
        upload_images,
        model_type,
        _num_steps,
        style_name,
        denoise_or_ip_sacle,
        _style_strength_ratio,
        cfg,
        seed_,
        id_length,
        general_prompt,
        negative_prompt,
        prompt_array,
        width,
        height,
        load_chars,
        lora,
        trigger_words, photomake_mode, use_kolor, use_flux, make_dual_only, kolor_face, pulid, story_maker,
        input_id_emb_s_dict, input_id_img_s_dict, input_id_emb_un_dict, input_id_cloth_dict, guidance, control_img,
        empty_emb_zero, miX_mode,use_cf,cf_scheduler
):  # Corrected font_choice usage
    
    if len(general_prompt.splitlines()) >= 3:
        raise "Support for more than three characters is temporarily unavailable due to VRAM limitations, but this issue will be resolved soon."
    # _model_type = "Photomaker" if _model_type == "Using Ref Images" else "original"
    
    if not use_kolor and not use_flux and not story_maker:
        if model_type == "img2img" and "img" not in general_prompt:
            raise 'if using normal SDXL img2img ,need add the triger word " img "  behind the class word you want to customize, such as: man img or woman img'
    
    global total_length, attn_procs, cur_model_type
    global write
    global cur_step, attn_count
    
    # load_chars = load_character_files_on_running(unet, character_files=char_files)
    
    prompts_origin = prompt_array.splitlines()
    prompts_origin = [i.strip() for i in prompts_origin]
    prompts_origin = [i for i in prompts_origin if '[' in i]  # 删除空行
    # print(prompts_origin)
    prompts = [prompt for prompt in prompts_origin if not len(extract_content_from_brackets(prompt)) >= 2]  # 剔除双角色
    
    add_trigger_words = " " + trigger_words + " style "
    if lora:
        if lora in lora_lightning_list:
            prompts = remove_punctuation_from_strings(prompts)
        else:
            prompts = remove_punctuation_from_strings(prompts)
            prompts = [item + add_trigger_words for item in prompts]
    
    global character_index_dict, invert_character_index_dict, ref_indexs_dict, ref_totals
    global character_dict
    
    character_dict, character_list = character_to_dict(general_prompt, lora, add_trigger_words)
    # print(character_dict)
    start_merge_step = int(float(_style_strength_ratio) / 100 * _num_steps)
    if start_merge_step > 30:
        start_merge_step = 30
    print(f"start_merge_step:{start_merge_step}")
    # generator = torch.Generator(device=device).manual_seed(seed_)
    clipped_prompts = prompts[:]
    nc_indexs = []
    for ind, prompt in enumerate(clipped_prompts):
        if "[NC]" in prompt:
            nc_indexs.append(ind)
            if ind < id_length:
                raise f"The first {id_length} row is id prompts, cannot use [NC]!"
    
    prompts = [
        prompt if "[NC]" not in prompt else prompt.replace("[NC]", "")
        for prompt in clipped_prompts
    ]
    
    if lora:
        if lora in lora_lightning_list:
            prompts = [
                prompt.rpartition("#")[0] if "#" in prompt else prompt for prompt in prompts
            ]
        else:
            prompts = [
                prompt.rpartition("#")[0] + add_trigger_words if "#" in prompt else prompt for prompt in prompts
            ]
    else:
        prompts = [
            prompt.rpartition("#")[0] if "#" in prompt else prompt for prompt in prompts
        ]
    img_mode = False
    if kolor_face or pulid or (story_maker and not make_dual_only) or model_type == "img2img":
        img_mode = True
    # id_prompts = prompts[:id_length]
    (
        character_index_dict,
        invert_character_index_dict,
        replace_prompts,
        ref_indexs_dict,
        ref_totals,
    ) = process_original_prompt(character_dict, prompts, id_length, img_mode)
    # print(character_index_dict,invert_character_index_dict,replace_prompts,ref_indexs_dict,ref_totals)
    # character_index_dict：{'[Taylor]': [0, 3], '[sam]': [1, 2]},if 1 role {'[Taylor]': [0, 1, 2]}
    # invert_character_index_dict:{0: ['[Taylor]'], 1: ['[sam]'], 2: ['[sam]'], 3: ['[Taylor]']},if 1 role  {0: ['[Taylor]'], 1: ['[Taylor]'], 2: ['[Taylor]']}
    # ref_indexs_dict:{'[Taylor]': [0, 3], '[sam]': [1, 2]},if 1 role {'[Taylor]': [0]}
    # ref_totals: [0, 3, 1, 2]  if 1 role [0]
    if model_type == "img2img":
        # _upload_images = [_upload_images]
        input_id_images_dict = {}
        if len(upload_images) != len(character_dict.keys()):
            raise f"You upload images({len(upload_images)}) is not equal to the number of characters({len(character_dict.keys())})!"
        for ind, img in enumerate(upload_images):
            input_id_images_dict[character_list[ind]] = [img]  # 已经pil转化了 不用load {a:[img],b:[img]}
            # input_id_images_dict[character_list[ind]] = [load_image(img)]
    # real_prompts = prompts[id_length:]
    # if device == "cuda":
    #     torch.cuda.empty_cache()
    write = True
    cur_step = 0
    attn_count = 0
    total_results = []
    id_images = []
    results_dict = {}
    p_num = 0
    
    global cur_character
    if not load_chars:
        for character_key in character_dict.keys():  # 先生成角色对应第一句场景提示词的图片,图生图是批次生成
            character_key_str = character_key
            cur_character = [character_key]
            ref_indexs = ref_indexs_dict[character_key]
            current_prompts = [replace_prompts[ref_ind] for ref_ind in ref_indexs]
            if model_type == "txt2img":
                setup_seed(seed_)
            generator = torch.Generator(device=device).manual_seed(seed_)
            cur_step = 0
            cur_positive_prompts, cur_negative_prompt = apply_style(
                style_name, current_prompts, negative_prompt
            )
            print(f"Sampler  {character_key_str} 's cur_positive_prompts :{cur_positive_prompts}")
            if model_type == "txt2img":
                if use_flux:
                    if not use_cf:
                        id_images = pipe(
                            prompt=cur_positive_prompts,
                            num_inference_steps=_num_steps,
                            guidance_scale=guidance,
                            output_type="pil",
                            max_sequence_length=256,
                            height=height,
                            width=width,
                            generator=generator
                        ).images
                    else:
                        cur_negative_prompt = [cur_negative_prompt]
                        cur_negative_prompt = cur_negative_prompt * len(cur_positive_prompts) if len(
                            cur_negative_prompt) != len(cur_positive_prompts) else cur_negative_prompt
                        id_images = []
                        cfg=1.0
                        for index, text in enumerate(cur_positive_prompts):
                            id_image = pipe.generate_image(
                                width=width,
                                height=height,
                                num_steps=_num_steps,
                                cfg=cfg,
                                guidance=guidance,
                                seed=seed_,
                                prompt=text,
                                negative_prompt=cur_negative_prompt[index],
                                cf_scheduler=cf_scheduler,
                                denoise=denoise_or_ip_sacle,
                                image=None
                            )
                            id_images.append(id_image)
                else:
                    if use_cf:
                        cur_negative_prompt = [cur_negative_prompt]
                        cur_negative_prompt = cur_negative_prompt * len(cur_positive_prompts) if len(
                            cur_negative_prompt) != len(cur_positive_prompts) else cur_negative_prompt
                        id_images = []
                        for index, text in enumerate(cur_positive_prompts):
                            id_image = pipe.generate_image(
                                width=width,
                                height=height,
                                num_steps=_num_steps,
                                cfg=cfg,
                                guidance=guidance,
                                seed=seed_,
                                prompt=text,
                                negative_prompt=cur_negative_prompt[index],
                                cf_scheduler=cf_scheduler,
                                denoise=denoise_or_ip_sacle,
                                image=None
                            )
                            id_images.append(id_image)
                    else:
                        if use_kolor:
                            cur_negative_prompt = [cur_negative_prompt]
                            cur_negative_prompt = cur_negative_prompt * len(cur_positive_prompts) if len(
                                cur_negative_prompt) != len(cur_positive_prompts) else cur_negative_prompt
                        id_images = pipe(
                            cur_positive_prompts,
                            num_inference_steps=_num_steps,
                            guidance_scale=cfg,
                            height=height,
                            width=width,
                            negative_prompt=cur_negative_prompt,
                            generator=generator
                        ).images
                    
            
            elif model_type == "img2img":
                if use_kolor:
                    cur_negative_prompt = [cur_negative_prompt]
                    cur_negative_prompt = cur_negative_prompt * len(cur_positive_prompts) if len(
                        cur_negative_prompt) != len(cur_positive_prompts) else cur_negative_prompt
                    if kolor_face:
                        crop_image = input_id_img_s_dict[character_key_str][0]
                        face_embeds = input_id_emb_s_dict[character_key_str][0]
                        face_embeds = face_embeds.to(device, dtype=torch.float16)
                        if id_length > 1:
                            id_images = []
                            for index, i in enumerate(cur_positive_prompts):
                                id_image = pipe(
                                    prompt=i,
                                    negative_prompt=cur_negative_prompt[index],
                                    height=height,
                                    width=width,
                                    num_inference_steps=_num_steps,
                                    guidance_scale=cfg,
                                    num_images_per_prompt=1,
                                    generator=generator,
                                    face_crop_image=crop_image,
                                    face_insightface_embeds=face_embeds,
                                ).images
                                id_images.append(id_image)
                        else:
                            id_images = pipe(
                                prompt=cur_positive_prompts,
                                negative_prompt=cur_negative_prompt,
                                height=height,
                                width=width,
                                num_inference_steps=_num_steps,
                                guidance_scale=cfg,
                                num_images_per_prompt=1,
                                generator=generator,
                                face_crop_image=crop_image,
                                face_insightface_embeds=face_embeds,
                            ).images
                    else:
                        pipe.set_ip_adapter_scale([denoise_or_ip_sacle])
                        id_images = pipe(
                            prompt=cur_positive_prompts,
                            ip_adapter_image=input_id_images_dict[character_key],
                            negative_prompt=cur_negative_prompt,
                            num_inference_steps=_num_steps,
                            height=height,
                            width=width,
                            guidance_scale=cfg,
                            num_images_per_prompt=1,
                            generator=generator,
                        ).images
                elif use_flux:
                    if pulid:
                        id_embeddings = input_id_emb_s_dict[character_key_str][0]
                        uncond_id_embeddings = input_id_emb_un_dict[character_key_str][0]
                        if id_length > 1:
                            id_images = []
                            for index, i in enumerate(cur_positive_prompts):
                                id_image = pipe.generate_image(
                                    prompt=i,
                                    seed=seed_,
                                    start_step=2,
                                    num_steps=_num_steps,
                                    height=height,
                                    width=width,
                                    id_embeddings=id_embeddings,
                                    uncond_id_embeddings=uncond_id_embeddings,
                                    id_weight=1,
                                    guidance=guidance,
                                    true_cfg=cfg,
                                    max_sequence_length=128,
                                )
                                id_images.append(id_image)
                    
                        else:
                            id_images = pipe.generate_image(
                                prompt=cur_positive_prompts,
                                seed=seed_,
                                start_step=2,
                                num_steps=_num_steps,
                                height=height,
                                width=width,
                                id_embeddings=id_embeddings,
                                uncond_id_embeddings=uncond_id_embeddings,
                                id_weight=1,
                                guidance=guidance,
                                true_cfg=cfg,
                                max_sequence_length=128,
                            )
                            id_images = [id_images]
                    elif use_cf:
                        cfg = 1.0
                        cur_negative_prompt = [cur_negative_prompt]
                        cur_negative_prompt = cur_negative_prompt * len(cur_positive_prompts) if len(cur_negative_prompt) != len(cur_positive_prompts) else cur_negative_prompt
                        id_images = []
                        for index, text in enumerate(cur_positive_prompts):
                            id_image = pipe.generate_image(
                                width=width,
                                height=height,
                                num_steps=_num_steps,
                                cfg=cfg,
                                guidance=guidance,
                                seed=seed_,
                                prompt=text,
                                negative_prompt=cur_negative_prompt[index],
                                cf_scheduler=cf_scheduler,
                                denoise=denoise_or_ip_sacle,
                                image=input_id_images_dict[character_key][0]
                            )
                            id_images.append(id_image)
                    else:
                        cfg = cfg if cfg <= 1 else cfg / 10 if 1 < cfg <= 10 else cfg / 100
                        id_images = pipe(
                            prompt=cur_positive_prompts,
                            image=input_id_images_dict[character_key],
                            strength=cfg,
                            latents=None,
                            num_inference_steps=_num_steps,
                            height=height,
                            width=width,
                            output_type="pil",
                            max_sequence_length=256,
                            guidance_scale=guidance,
                            generator=generator,
                        ).images
                
                elif story_maker and not make_dual_only:
                    img = input_id_images_dict[character_key][0]
                    # print(character_key_str,input_id_images_dict)
                    mask_image = input_id_img_s_dict[character_key_str][0]
                    face_info = input_id_emb_s_dict[character_key_str][0]
                    cloth_info = None
                    if isinstance(control_img, torch.Tensor):
                        cloth_info = input_id_cloth_dict[character_key_str][0]
                    cur_negative_prompt = [cur_negative_prompt]
                    cur_negative_prompt = cur_negative_prompt * len(cur_positive_prompts) if len(
                        cur_negative_prompt) != len(cur_positive_prompts) else cur_negative_prompt
                    if id_length > 1:
                        id_images = []
                        for index, i in enumerate(cur_positive_prompts):
                            id_image = pipe(
                                image=img,
                                mask_image=mask_image,
                                face_info=face_info,
                                prompt=i,
                                negative_prompt=cur_negative_prompt[index],
                                ip_adapter_scale=denoise_or_ip_sacle, lora_scale=0.8,
                                num_inference_steps=_num_steps,
                                guidance_scale=cfg,
                                height=height, width=width,
                                generator=generator,
                                cloth=cloth_info,
                            ).images
                            id_images.append(id_image)
                    else:
                        id_images = pipe(
                            image=img,
                            mask_image=mask_image,
                            face_info=face_info,
                            prompt=cur_positive_prompts,
                            negative_prompt=cur_negative_prompt,
                            ip_adapter_scale=denoise_or_ip_sacle, lora_scale=0.8,
                            num_inference_steps=_num_steps,
                            guidance_scale=cfg,
                            height=height, width=width,
                            generator=generator,
                            cloth=cloth_info,
                        ).images
                
                else:
                    if use_cf:
                        cur_negative_prompt = [cur_negative_prompt]
                        cur_negative_prompt = cur_negative_prompt * len(cur_positive_prompts) if len(
                            cur_negative_prompt) != len(cur_positive_prompts) else cur_negative_prompt
                        id_images = []
                        for index, text in enumerate(cur_positive_prompts):
                            id_image = pipe.generate_image(
                                width=width,
                                height=height,
                                num_steps=_num_steps,
                                cfg=cfg,
                                guidance=guidance,
                                seed=seed_,
                                prompt=text,
                                negative_prompt=cur_negative_prompt[index],
                                cf_scheduler=cf_scheduler,
                                denoise=denoise_or_ip_sacle,
                                image=input_id_images_dict[character_key][0]
                            )
                            id_images.append(id_image)
                    else:
                        if photomake_mode == "v2":
                            id_embeds = input_id_emb_s_dict[character_key_str][0]
                            id_images = pipe(
                                cur_positive_prompts,
                                input_id_images=input_id_images_dict[character_key],
                                num_inference_steps=_num_steps,
                                guidance_scale=cfg,
                                start_merge_step=start_merge_step,
                                height=height,
                                width=width,
                                negative_prompt=cur_negative_prompt,
                                id_embeds=id_embeds,
                                generator=generator
                            ).images
                        else:
                            # print("v1 mode,load_chars", cur_positive_prompts, negative_prompt,character_key )
                            id_images = pipe(
                                cur_positive_prompts,
                                input_id_images=input_id_images_dict[character_key],
                                num_inference_steps=_num_steps,
                                guidance_scale=cfg,
                                start_merge_step=start_merge_step,
                                height=height,
                                width=width,
                                negative_prompt=cur_negative_prompt,
                                generator=generator
                            ).images
                    
            else:
                raise NotImplementedError(
                    "You should choice between original and Photomaker!",
                    f"But you choice {model_type}",
                )
            p_num += 1
            # total_results = id_images + total_results
            # yield total_results
            if story_maker and not make_dual_only and id_length > 1 and model_type == "img2img":
                for index, ind in enumerate(character_index_dict[character_key]):
                    results_dict[ref_totals[ind]] = id_images[index]
            elif pulid and id_length > 1 and model_type == "img2img":
                for index, ind in enumerate(character_index_dict[character_key]):
                    results_dict[ref_totals[ind]] = id_images[index]
            elif kolor_face and id_length > 1 and model_type == "img2img":
                for index, ind in enumerate(character_index_dict[character_key]):
                    results_dict[ref_totals[ind]] = id_images[index]
            elif use_flux and use_cf and id_length > 1 and model_type == "img2img":
                for index, ind in enumerate(character_index_dict[character_key]):
                    results_dict[ref_totals[ind]] = id_images[index]
            elif use_cf and not use_flux and id_length > 1 and model_type == "img2img":
                for index, ind in enumerate(character_index_dict[character_key]):
                    results_dict[ref_totals[ind]] = id_images[index]
            else:
                for ind, img in enumerate(id_images):
                    results_dict[ref_indexs[ind]] = img
            # real_images = []
            #print(results_dict)
            yield [results_dict[ind] for ind in results_dict.keys()]
            
    write = False
    if not load_chars:
        real_prompts_inds = [
            ind for ind in range(len(prompts)) if ind not in ref_totals
        ]
    else:
        real_prompts_inds = [ind for ind in range(len(prompts))]
    
    real_prompt_no, negative_prompt_style = apply_style_positive(style_name, "real_prompt")
    negative_prompt = str(negative_prompt) + str(negative_prompt_style)
    # print(f"real_prompts_inds is {real_prompts_inds}")
    for real_prompts_ind in real_prompts_inds:  #
        real_prompt = replace_prompts[real_prompts_ind]
        cur_character = get_ref_character(prompts[real_prompts_ind], character_dict)
        
        if model_type == "txt2img":
            setup_seed(seed_)
        generator = torch.Generator(device=device).manual_seed(seed_)
        
        if len(cur_character) > 1 and model_type == "img2img":
            raise "Temporarily Not Support Multiple character in Ref Image Mode!"
        cur_step = 0
        real_prompt, negative_prompt_style_no = apply_style_positive(style_name, real_prompt)
        print(f"Sample real_prompt : {real_prompt}")
        if model_type == "txt2img":
            # print(results_dict,real_prompts_ind)
            if use_flux:
                if not use_cf:
                    results_dict[real_prompts_ind] = pipe(
                        prompt=real_prompt,
                        num_inference_steps=_num_steps,
                        guidance_scale=guidance,
                        output_type="pil",
                        max_sequence_length=256,
                        height=height,
                        width=width,
                        generator=torch.Generator("cpu").manual_seed(seed_)
                    ).images[0]
                else:
                    cfg = 1.0
                    results_dict[real_prompts_ind] = pipe.generate_image(
                        width=width,
                        height=height,
                        num_steps=_num_steps,
                        cfg=cfg,
                        guidance=guidance,
                        seed=seed_,
                        prompt=real_prompt,
                        negative_prompt=negative_prompt,
                        cf_scheduler=cf_scheduler,
                        denoise=denoise_or_ip_sacle,
                        image=None
                    )
            else:
                if use_cf:
                    results_dict[real_prompts_ind] = pipe.generate_image(
                        width=width,
                        height=height,
                        num_steps=_num_steps,
                        cfg=cfg,
                        guidance=guidance,
                        seed=seed_,
                        prompt=real_prompt,
                        negative_prompt=negative_prompt,
                        cf_scheduler=cf_scheduler,
                        denoise=denoise_or_ip_sacle,
                        image=None
                    )
                else:
                    results_dict[real_prompts_ind] = pipe(
                        real_prompt,
                        num_inference_steps=_num_steps,
                        guidance_scale=cfg,
                        height=height,
                        width=width,
                        negative_prompt=negative_prompt,
                        generator=generator,
                    ).images[0]
                
        elif model_type == "img2img":
            empty_img = Image.new('RGB', (height, width), (255, 255, 255))
            if use_kolor:
                if kolor_face:
                    empty_image = Image.new('RGB', (336, 336), (255, 255, 255))
                    crop_image = input_id_img_s_dict[
                        cur_character[0]] if real_prompts_ind not in nc_indexs else empty_image
                    face_embeds = input_id_emb_s_dict[cur_character[0]][
                        0] if real_prompts_ind not in nc_indexs else empty_emb_zero
                    face_embeds = face_embeds.to(device, dtype=torch.float16)
                    results_dict[real_prompts_ind] = pipe(
                        prompt=real_prompt,
                        negative_prompt=negative_prompt,
                        height=height,
                        width=width,
                        num_inference_steps=_num_steps,
                        guidance_scale=cfg,
                        num_images_per_prompt=1,
                        generator=generator,
                        face_crop_image=crop_image,
                        face_insightface_embeds=face_embeds,
                    ).images[0]
                else:
                    results_dict[real_prompts_ind] = pipe(
                        prompt=real_prompt,
                        ip_adapter_image=(
                            input_id_images_dict[cur_character[0]]
                            if real_prompts_ind not in nc_indexs
                            else empty_img
                        ),
                        negative_prompt=negative_prompt,
                        height=height,
                        width=width,
                        num_inference_steps=_num_steps,
                        guidance_scale=cfg,
                        num_images_per_prompt=1,
                        generator=generator,
                        nc_flag=True if real_prompts_ind in nc_indexs else False,  # nc_flag，用索引标记，主要控制非角色人物的生成，默认false
                    ).images[0]
            elif use_flux:
                if pulid:
                    id_embeddings = input_id_emb_s_dict[cur_character[0]][
                        0] if real_prompts_ind not in nc_indexs else empty_emb_zero
                    uncond_id_embeddings = input_id_emb_un_dict[cur_character[0]][
                        0] if real_prompts_ind not in nc_indexs else empty_emb_zero
                    results_dict[real_prompts_ind] = pipe.generate_image(
                        prompt=real_prompt,
                        seed=seed_,
                        start_step=2,
                        num_steps=_num_steps,
                        height=height,
                        width=width,
                        id_embeddings=id_embeddings,
                        uncond_id_embeddings=uncond_id_embeddings,
                        id_weight=1,
                        guidance=guidance,
                        true_cfg=1.0,
                        max_sequence_length=128,
                    )
                if use_cf:
                    cfg = 1.0
                    results_dict[real_prompts_ind] = pipe.generate_image(
                        width=width,
                        height=height,
                        num_steps=_num_steps,
                        cfg=cfg,
                        guidance=guidance,
                        seed=seed_,
                        prompt=real_prompt,
                        negative_prompt=negative_prompt,
                        cf_scheduler=cf_scheduler,
                        denoise=denoise_or_ip_sacle,
                        image=(input_id_images_dict[cur_character[0]]
                            if real_prompts_ind not in nc_indexs
                            else empty_img
                        )
                    )
                else:
                    cfg = cfg if cfg <= 1 else cfg/10 if 1<cfg<=10 else cfg/100
                    results_dict[real_prompts_ind] = pipe(
                        prompt=real_prompt,
                        image=(
                            input_id_images_dict[cur_character[0]]
                            if real_prompts_ind not in nc_indexs
                            else empty_img
                        ),
                        latents=None,
                        strength=cfg,
                        num_inference_steps=_num_steps,
                        height=height,
                        width=width,
                        output_type="pil",
                        max_sequence_length=256,
                        guidance_scale=guidance,
                        generator=generator,
                    ).images[0]
            elif story_maker and not make_dual_only:
                cloth_info = None
                if isinstance(control_img, torch.Tensor):
                    cloth_info = input_id_cloth_dict[cur_character[0]][0]
                mask_image = input_id_img_s_dict[cur_character[0]][0]
                img_2 = input_id_images_dict[cur_character[0]][0] if real_prompts_ind not in nc_indexs else empty_img
                face_info = input_id_emb_s_dict[cur_character[0]][
                    0] if real_prompts_ind not in nc_indexs else empty_emb_zero
                
                results_dict[real_prompts_ind] = pipe(
                    image=img_2,
                    mask_image=mask_image,
                    face_info=face_info,
                    prompt=real_prompt,
                    negative_prompt=negative_prompt,
                    ip_adapter_scale=denoise_or_ip_sacle, lora_scale=0.8,
                    num_inference_steps=_num_steps,
                    guidance_scale=cfg,
                    height=height, width=width,
                    generator=generator,
                    cloth=cloth_info,
                ).images[0]
            else:
                if use_cf:
                    results_dict[real_prompts_ind] = pipe.generate_image(
                        width=width,
                        height=height,
                        num_steps=_num_steps,
                        cfg=cfg,
                        guidance=guidance,
                        seed=seed_,
                        prompt=real_prompt,
                        negative_prompt=negative_prompt,
                        cf_scheduler=cf_scheduler,
                        denoise=denoise_or_ip_sacle,
                        image=(input_id_images_dict[cur_character[0]]
                               if real_prompts_ind not in nc_indexs
                               else empty_img
                               )
                        )
                else:
                    if photomake_mode == "v2":
                        # V2版本必须要有id_embeds，只能用input_id_images作为风格参考
                        print(cur_character)
                        id_embeds = input_id_emb_s_dict[cur_character[0]][
                            0] if real_prompts_ind not in nc_indexs else empty_emb_zero
                        results_dict[real_prompts_ind] = pipe(
                            real_prompt,
                            input_id_images=(
                                input_id_images_dict[cur_character[0]]
                                if real_prompts_ind not in nc_indexs
                                else input_id_images_dict[character_list[0]]
                            ),
                            num_inference_steps=_num_steps,
                            guidance_scale=cfg,
                            start_merge_step=start_merge_step,
                            height=height,
                            width=width,
                            negative_prompt=negative_prompt,
                            generator=generator,
                            id_embeds=id_embeds,
                            nc_flag=True if real_prompts_ind in nc_indexs else False,
                        ).images[0]
                    else:
                        # print(real_prompts_ind, real_prompt, "v1 mode", )
                        results_dict[real_prompts_ind] = pipe(
                            real_prompt,
                            input_id_images=(
                                input_id_images_dict[cur_character[0]]
                                if real_prompts_ind not in nc_indexs
                                else input_id_images_dict[character_list[0]]
                            ),
                            num_inference_steps=_num_steps,
                            guidance_scale=cfg,
                            start_merge_step=start_merge_step,
                            height=height,
                            width=width,
                            negative_prompt=negative_prompt,
                            generator=generator,
                            nc_flag=True if real_prompts_ind in nc_indexs else False,
                            # nc_flag，用索引标记，主要控制非角色人物的生成，默认false
                        ).images[0]
                
        else:
            raise NotImplementedError(
                "You should choice between original and Photomaker!",
                f"But you choice {model_type}",
            )
        yield [results_dict[ind] for ind in results_dict.keys()]
    total_results = [results_dict[ind] for ind in range(len(prompts))]
    torch.cuda.empty_cache()
    yield total_results