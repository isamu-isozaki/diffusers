import gc
import time
import unittest

import numpy as np
import torch
from huggingface_hub import hf_hub_download
from packaging import version
from PIL import Image
from transformers import CLIPConfig, CLIPFeatureExtractor, CLIPModel, CLIPTokenizer

from diffusers import (
    AutoencoderKL,
    DDIMScheduler,
    DPMSolverMultistepScheduler,
    EulerAncestralDiscreteScheduler,
    EulerDiscreteScheduler,
    LMSDiscreteScheduler,
    PNDMScheduler,
    RDMPipeline,
    UNet2DConditionModel,
)
from diffusers.utils import load_numpy, nightly, slow, torch_device
from diffusers.utils.testing_utils import require_torch_gpu

from ..pipeline_params import TEXT_TO_IMAGE_BATCH_PARAMS, TEXT_TO_IMAGE_PARAMS
from ..test_pipelines_common import PipelineTesterMixin


torch.backends.cuda.matmul.allow_tf32 = False


class RDMPipelineFastTests(PipelineTesterMixin, unittest.TestCase):
    pipeline_class = RDMPipeline
    params = TEXT_TO_IMAGE_PARAMS - {
        "negative_prompt",
        "negative_prompt_embeds",
        "cross_attention_kwargs",
        "prompt_embeds",
    }
    batch_params = TEXT_TO_IMAGE_BATCH_PARAMS

    def get_dummy_components(self):
        torch.manual_seed(0)
        unet = UNet2DConditionModel(
            block_out_channels=(32, 64),
            layers_per_block=2,
            sample_size=32,
            in_channels=4,
            out_channels=4,
            down_block_types=("DownBlock2D", "CrossAttnDownBlock2D"),
            up_block_types=("CrossAttnUpBlock2D", "UpBlock2D"),
            cross_attention_dim=64,
        )
        scheduler = DDIMScheduler(
            beta_start=0.00085,
            beta_end=0.012,
            beta_schedule="scaled_linear",
            clip_sample=False,
            set_alpha_to_one=False,
        )
        torch.manual_seed(0)
        vae = AutoencoderKL(
            block_out_channels=[32, 64],
            in_channels=3,
            out_channels=3,
            down_block_types=["DownEncoderBlock2D", "DownEncoderBlock2D"],
            up_block_types=["UpDecoderBlock2D", "UpDecoderBlock2D"],
            latent_channels=4,
        )
        torch.manual_seed(0)
        clip_config = CLIPConfig.from_pretrained("hf-internal-testing/tiny-random-clip")
        clip_config.text_config.vocab_size = 49408

        clip = CLIPModel.from_pretrained(
            "hf-internal-testing/tiny-random-clip", config=clip_config, ignore_mismatched_sizes=True
        )
        tokenizer = CLIPTokenizer.from_pretrained("hf-internal-testing/tiny-random-clip")
        feature_extractor = CLIPFeatureExtractor.from_pretrained(
            "hf-internal-testing/tiny-random-clip", size={"shortest_edge": 30}, crop_size={"height": 30, "width": 30}
        )

        components = {
            "unet": unet,
            "scheduler": scheduler,
            "vae": vae,
            "clip": clip,
            "tokenizer": tokenizer,
            "feature_extractor": feature_extractor,
        }
        return components

    def get_dummy_inputs(self, device, seed=0):
        if str(device).startswith("mps"):
            generator = torch.manual_seed(seed)
        else:
            generator = torch.Generator(device=device).manual_seed(seed)
        # To work with tiny clip, the prompt tokens need to be in the range of 0 to 100 when tokenized
        inputs = {
            "prompt": "A painting of a squirrel eating a burger",
            "retrieved_images": [Image.fromarray(np.zeros((30, 30, 3)).astype(np.uint8))],
            "generator": generator,
            "num_inference_steps": 2,
            "guidance_scale": 6.0,
            "output_type": "numpy",
            "height": 64,
            "width": 64,
        }
        return inputs

    def test_rdm_ddim(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator

        components = self.get_dummy_components()
        sd_pipe = RDMPipeline(**components)
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        output = sd_pipe(**inputs)
        image = output.images

        image_slice = image[0, -3:, -3:, -1]
        assert image.shape == (1, 64, 64, 3)
        expected_slice = np.array([0.489, 0.591, 0.478, 0.505, 0.587, 0.481, 0.536, 0.493, 0.478])
        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-2

    def test_rdm_ddim_factor_8(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator

        components = self.get_dummy_components()
        sd_pipe = RDMPipeline(**components)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        inputs["height"] = 136
        inputs["width"] = 136
        output = sd_pipe(**inputs)
        image = output.images

        image_slice = image[0, -3:, -3:, -1]
        assert image.shape == (1, 136, 136, 3)
        expected_slice = np.array([0.554, 0.581, 0.577, 0.509, 0.455, 0.421, 0.485, 0.452, 0.434])

        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-2

    def test_rdm_pndm(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator
        components = self.get_dummy_components()
        sd_pipe = RDMPipeline(**components)
        sd_pipe.scheduler = PNDMScheduler(skip_prk_steps=True)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        output = sd_pipe(**inputs)
        image = output.images
        image_slice = image[0, -3:, -3:, -1]

        assert image.shape == (1, 64, 64, 3)
        expected_slice = np.array([0.445, 0.564, 0.476, 0.502, 0.605, 0.495, 0.559, 0.498, 0.499])

        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-2

    def test_rdm_k_lms(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator

        components = self.get_dummy_components()
        sd_pipe = RDMPipeline(**components)
        sd_pipe.scheduler = LMSDiscreteScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        output = sd_pipe(**inputs)
        image = output.images
        image_slice = image[0, -3:, -3:, -1]

        assert image.shape == (1, 64, 64, 3)
        expected_slice = np.array([0.417, 0.549, 0.462, 0.498, 0.610, 0.502, 0.571, 0.504, 0.502])

        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-2

    def test_rdm_k_euler_ancestral(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator

        components = self.get_dummy_components()
        sd_pipe = RDMPipeline(**components)
        sd_pipe.scheduler = EulerAncestralDiscreteScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        output = sd_pipe(**inputs)
        image = output.images
        image_slice = image[0, -3:, -3:, -1]

        assert image.shape == (1, 64, 64, 3)
        expected_slice = np.array([0.417, 0.549, 0.462, 0.498, 0.610, 0.502, 0.570, 0.504, 0.502])

        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-2

    def test_rdm_k_euler(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator

        components = self.get_dummy_components()
        sd_pipe = RDMPipeline(**components)
        sd_pipe.scheduler = EulerDiscreteScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        output = sd_pipe(**inputs)
        image = output.images
        image_slice = image[0, -3:, -3:, -1]

        assert image.shape == (1, 64, 64, 3)
        expected_slice = np.array([0.417, 0.549, 0.462, 0.498, 0.610, 0.502, 0.571, 0.504, 0.502])

        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-2

    def test_rdm_vae_slicing(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator
        components = self.get_dummy_components()
        components["scheduler"] = LMSDiscreteScheduler.from_config(components["scheduler"].config)
        sd_pipe = RDMPipeline(**components)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        image_count = 4

        inputs = self.get_dummy_inputs(device)
        inputs["prompt"] = [inputs["prompt"]] * image_count
        output_1 = sd_pipe(**inputs)

        # make sure sliced vae decode yields the same result
        sd_pipe.enable_vae_slicing()
        inputs = self.get_dummy_inputs(device)
        inputs["prompt"] = [inputs["prompt"]] * image_count
        output_2 = sd_pipe(**inputs)

        # there is a small discrepancy at image borders vs. full batch decode
        assert np.abs(output_2.images.flatten() - output_1.images.flatten()).max() < 3e-3

    def test_rdm_vae_tiling(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator
        components = self.get_dummy_components()

        # make sure here that pndm scheduler skips prk
        sd_pipe = RDMPipeline(**components)
        sd_pipe = sd_pipe.to(device)
        sd_pipe.set_progress_bar_config(disable=None)

        prompt = "A painting of a squirrel eating a burger"

        # Test that tiled decode at 512x512 yields the same result as the non-tiled decode
        generator = torch.Generator(device=device).manual_seed(0)
        output_1 = sd_pipe(
            [prompt],
            generator=generator,
            guidance_scale=6.0,
            num_inference_steps=2,
            height=64,
            width=64,
            output_type="np",
        )

        # make sure tiled vae decode yields the same result
        sd_pipe.enable_vae_tiling()
        generator = torch.Generator(device=device).manual_seed(0)
        output_2 = sd_pipe(
            [prompt],
            generator=generator,
            guidance_scale=6.0,
            num_inference_steps=2,
            height=64,
            width=64,
            output_type="np",
        )

        assert np.abs(output_2.images.flatten() - output_1.images.flatten()).max() < 5e-1

        # test that tiled decode works with various shapes
        shapes = [(1, 4, 73, 97), (1, 4, 97, 73), (1, 4, 49, 65), (1, 4, 65, 49)]
        for shape in shapes:
            zeros = torch.zeros(shape).to(device)
            sd_pipe.vae.decode(zeros)

    def test_rdm_with_retrieved_images(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator

        components = self.get_dummy_components()
        sd_pipe = RDMPipeline(**components)
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        inputs["retrieved_images"] = [Image.fromarray(np.zeros((64, 64, 3)).astype(np.uint8))]
        output = sd_pipe(**inputs)
        image = output.images

        image_slice = image[0, -3:, -3:, -1]

        assert image.shape == (1, 64, 64, 3)
        expected_slice = np.array([0.489, 0.591, 0.478, 0.505, 0.587, 0.481, 0.536, 0.493, 0.478])

        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-2


@slow
@require_torch_gpu
class RDMPipelineSlowTests(unittest.TestCase):
    def tearDown(self):
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()

    def get_inputs(self, device, generator_device="cpu", dtype=torch.float32, seed=0):
        generator = torch.Generator(device=generator_device).manual_seed(seed)
        latents = np.random.RandomState(seed).standard_normal((1, 4, 64, 64))
        latents = torch.from_numpy(latents).to(device=device, dtype=dtype)
        inputs = {
            "prompt": "a photograph of an astronaut riding a horse",
            "latents": latents,
            "generator": generator,
            "num_inference_steps": 3,
            "guidance_scale": 7.5,
            "output_type": "numpy",
        }
        return inputs

    def test_rdm_pndm(self):
        sd_pipe = RDMPipeline.from_pretrained("fusing/rdm")
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images
        image_slice = image[0, -3:, -3:, -1].flatten()

        assert image.shape == (1, 512, 512, 3)
        expected_slice = np.array([0.57400, 0.47841, 0.31625, 0.63583, 0.58306, 0.55056, 0.50825, 0.56306, 0.55748])
        assert np.abs(image_slice - expected_slice).max() < 1e-4

    def test_rdm_ddim(self):
        sd_pipe = RDMPipeline.from_pretrained("fusing/rdm")
        sd_pipe.scheduler = DDIMScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images
        image_slice = image[0, -3:, -3:, -1].flatten()

        assert image.shape == (1, 512, 512, 3)
        expected_slice = np.array([0.38019, 0.28647, 0.27321, 0.40377, 0.38290, 0.35446, 0.39218, 0.38165, 0.42239])
        assert np.abs(image_slice - expected_slice).max() < 1e-4

    def test_rdm_lms(self):
        sd_pipe = RDMPipeline.from_pretrained("fusing/rdm")
        sd_pipe.scheduler = LMSDiscreteScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images
        image_slice = image[0, -3:, -3:, -1].flatten()

        assert image.shape == (1, 512, 512, 3)
        expected_slice = np.array([0.10542, 0.09620, 0.07332, 0.09015, 0.09382, 0.07597, 0.08496, 0.07806, 0.06455])
        assert np.abs(image_slice - expected_slice).max() < 1e-4

    def test_rdm_dpm(self):
        sd_pipe = RDMPipeline.from_pretrained("fusing/rdm")
        sd_pipe.scheduler = DPMSolverMultistepScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images
        image_slice = image[0, -3:, -3:, -1].flatten()

        assert image.shape == (1, 512, 512, 3)
        expected_slice = np.array([0.03503, 0.03494, 0.01087, 0.03128, 0.02552, 0.00803, 0.00742, 0.00372, 0.00000])
        assert np.abs(image_slice - expected_slice).max() < 1e-4

    def test_rdm_attention_slicing(self):
        torch.cuda.reset_peak_memory_stats()
        pipe = RDMPipeline.from_pretrained("fusing/rdm", torch_dtype=torch.float16)
        pipe = pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)

        # enable attention slicing
        pipe.enable_attention_slicing()
        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        image_sliced = pipe(**inputs).images

        mem_bytes = torch.cuda.max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()
        # make sure that less than 3.75 GB is allocated
        assert mem_bytes < 3.75 * 10**9

        # disable slicing
        pipe.disable_attention_slicing()
        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        image = pipe(**inputs).images

        # make sure that more than 3.75 GB is allocated
        mem_bytes = torch.cuda.max_memory_allocated()
        assert mem_bytes > 3.75 * 10**9
        assert np.abs(image_sliced - image).max() < 1e-3

    def test_rdm_vae_slicing(self):
        torch.cuda.reset_peak_memory_stats()
        pipe = RDMPipeline.from_pretrained("fusing/rdm", torch_dtype=torch.float16)
        pipe = pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing()

        # enable vae slicing
        pipe.enable_vae_slicing()
        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        inputs["prompt"] = [inputs["prompt"]] * 4
        inputs["latents"] = torch.cat([inputs["latents"]] * 4)
        image_sliced = pipe(**inputs).images

        mem_bytes = torch.cuda.max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()
        # make sure that less than 4 GB is allocated
        assert mem_bytes < 4e9

        # disable vae slicing
        pipe.disable_vae_slicing()
        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        inputs["prompt"] = [inputs["prompt"]] * 4
        inputs["latents"] = torch.cat([inputs["latents"]] * 4)
        image = pipe(**inputs).images

        # make sure that more than 4 GB is allocated
        mem_bytes = torch.cuda.max_memory_allocated()
        assert mem_bytes > 4e9
        # There is a small discrepancy at the image borders vs. a fully batched version.
        assert np.abs(image_sliced - image).max() < 1e-2

    def test_rdm_vae_tiling(self):
        torch.cuda.reset_peak_memory_stats()
        model_id = "fusing/rdm"
        pipe = RDMPipeline.from_pretrained(model_id, revision="fp16", torch_dtype=torch.float16)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing()
        pipe.unet = pipe.unet.to(memory_format=torch.channels_last)
        pipe.vae = pipe.vae.to(memory_format=torch.channels_last)

        prompt = "a photograph of an astronaut riding a horse"

        # enable vae tiling
        pipe.enable_vae_tiling()
        pipe.enable_model_cpu_offload()
        generator = torch.Generator(device="cpu").manual_seed(0)
        output_chunked = pipe(
            [prompt],
            width=1024,
            height=1024,
            generator=generator,
            guidance_scale=7.5,
            num_inference_steps=2,
            output_type="numpy",
        )
        image_chunked = output_chunked.images

        mem_bytes = torch.cuda.max_memory_allocated()

        # disable vae tiling
        pipe.disable_vae_tiling()
        generator = torch.Generator(device="cpu").manual_seed(0)
        output = pipe(
            [prompt],
            width=1024,
            height=1024,
            generator=generator,
            guidance_scale=7.5,
            num_inference_steps=2,
            output_type="numpy",
        )
        image = output.images

        assert mem_bytes < 1e10
        assert np.abs(image_chunked.flatten() - image.flatten()).max() < 1e-2

    def test_rdm_fp16_vs_autocast(self):
        # this test makes sure that the original model with autocast
        # and the new model with fp16 yield the same result
        pipe = RDMPipeline.from_pretrained("fusing/rdm", torch_dtype=torch.float16)
        pipe = pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        image_fp16 = pipe(**inputs).images

        with torch.autocast(torch_device):
            inputs = self.get_inputs(torch_device)
            image_autocast = pipe(**inputs).images

        # Make sure results are close enough
        diff = np.abs(image_fp16.flatten() - image_autocast.flatten())
        # They ARE different since ops are not run always at the same precision
        # however, they should be extremely close.
        assert diff.mean() < 2e-2

    def test_rdm_intermediate_state(self):
        number_of_steps = 0

        def callback_fn(step: int, timestep: int, latents: torch.FloatTensor) -> None:
            callback_fn.has_been_called = True
            nonlocal number_of_steps
            number_of_steps += 1
            if step == 1:
                latents = latents.detach().cpu().numpy()
                assert latents.shape == (1, 4, 64, 64)
                latents_slice = latents[0, -3:, -3:, -1]
                expected_slice = np.array(
                    [-0.5693, -0.3018, -0.9746, 0.0518, -0.8770, 0.7559, -1.7402, 0.1022, 1.1582]
                )

                assert np.abs(latents_slice.flatten() - expected_slice).max() < 5e-2
            elif step == 2:
                latents = latents.detach().cpu().numpy()
                assert latents.shape == (1, 4, 64, 64)
                latents_slice = latents[0, -3:, -3:, -1]
                expected_slice = np.array(
                    [-0.1958, -0.2993, -1.0166, -0.5005, -0.4810, 0.6162, -0.9492, 0.6621, 1.4492]
                )

                assert np.abs(latents_slice.flatten() - expected_slice).max() < 5e-2

        callback_fn.has_been_called = False

        pipe = RDMPipeline.from_pretrained("fusing/rdm", torch_dtype=torch.float16)
        pipe = pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing()

        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        pipe(**inputs, callback=callback_fn, callback_steps=1)
        assert callback_fn.has_been_called
        assert number_of_steps == inputs["num_inference_steps"]

    def test_rdm_low_cpu_mem_usage(self):
        pipeline_id = "fusing/rdm"

        start_time = time.time()
        pipeline_low_cpu_mem_usage = RDMPipeline.from_pretrained(pipeline_id, torch_dtype=torch.float16)
        pipeline_low_cpu_mem_usage.to(torch_device)
        low_cpu_mem_usage_time = time.time() - start_time

        start_time = time.time()
        _ = RDMPipeline.from_pretrained(pipeline_id, torch_dtype=torch.float16, low_cpu_mem_usage=False)
        normal_load_time = time.time() - start_time

        assert 2 * low_cpu_mem_usage_time < normal_load_time

    def test_rdm_pipeline_with_sequential_cpu_offloading(self):
        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()

        pipe = RDMPipeline.from_pretrained("fusing/rdm", torch_dtype=torch.float16)
        pipe = pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing(1)
        pipe.enable_sequential_cpu_offload()

        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        _ = pipe(**inputs)

        mem_bytes = torch.cuda.max_memory_allocated()
        # make sure that less than 2.8 GB is allocated
        assert mem_bytes < 2.8 * 10**9

    def test_rdm_pipeline_with_model_offloading(self):
        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()

        inputs = self.get_inputs(torch_device, dtype=torch.float16)

        # Normal inference

        pipe = RDMPipeline.from_pretrained(
            "fusing/rdm",
            torch_dtype=torch.float16,
        )
        pipe.unet.set_default_attn_processor()
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        outputs = pipe(**inputs)
        mem_bytes = torch.cuda.max_memory_allocated()

        # With model offloading

        # Reload but don't move to cuda
        pipe = RDMPipeline.from_pretrained(
            "fusing/rdm",
            torch_dtype=torch.float16,
        )
        pipe.unet.set_default_attn_processor()

        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()

        pipe.enable_model_cpu_offload()
        pipe.set_progress_bar_config(disable=None)
        inputs = self.get_inputs(torch_device, dtype=torch.float16)

        outputs_offloaded = pipe(**inputs)
        mem_bytes_offloaded = torch.cuda.max_memory_allocated()

        assert np.abs(outputs.images - outputs_offloaded.images).max() < 1e-3
        assert mem_bytes_offloaded < mem_bytes
        assert mem_bytes_offloaded < 3.5 * 10**9
        for module in pipe.text_encoder, pipe.unet, pipe.vae:
            assert module.device == torch.device("cpu")

        # With attention slicing
        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()

        pipe.enable_attention_slicing()
        _ = pipe(**inputs)
        mem_bytes_slicing = torch.cuda.max_memory_allocated()

        assert mem_bytes_slicing < mem_bytes_offloaded
        assert mem_bytes_slicing < 3 * 10**9

    def test_rdm_textual_inversion(self):
        pipe = RDMPipeline.from_pretrained("fusing/rdm")
        pipe.load_textual_inversion("sd-concepts-library/low-poly-hd-logos-icons")

        a111_file = hf_hub_download("hf-internal-testing/text_inv_embedding_a1111_format", "winter_style.pt")
        a111_file_neg = hf_hub_download(
            "hf-internal-testing/text_inv_embedding_a1111_format", "winter_style_negative.pt"
        )
        pipe.load_textual_inversion(a111_file)
        pipe.load_textual_inversion(a111_file_neg)
        pipe.to("cuda")

        generator = torch.Generator(device="cpu").manual_seed(1)

        prompt = "An logo of a turtle in strong Style-Winter with <low-poly-hd-logos-icons>"
        neg_prompt = "Style-Winter-neg"

        image = pipe(prompt=prompt, negative_prompt=neg_prompt, generator=generator, output_type="np").images[0]
        expected_image = load_numpy(
            "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/text_inv/winter_logo_style.npy"
        )

        max_diff = np.abs(expected_image - image).max()
        assert max_diff < 5e-2

    def test_rdm_compile(self):
        if version.parse(torch._version_) < version.parse("2.0"):
            print(f"Test `test_rdm_ddim` is skipped because {torch._version_} is < 2.0")
            return

        sd_pipe = RDMPipeline.from_pretrained("fusing/rdm")
        sd_pipe.scheduler = DDIMScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe = sd_pipe.to(torch_device)

        sd_pipe.unet.to(memory_format=torch.channels_last)
        sd_pipe.unet = torch.compile(sd_pipe.unet, mode="reduce-overhead", fullgraph=True)

        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images
        image_slice = image[0, -3:, -3:, -1].flatten()

        assert image.shape == (1, 512, 512, 3)
        expected_slice = np.array([0.489, 0.591, 0.478, 0.505, 0.587, 0.481, 0.536, 0.493, 0.478])
        assert np.abs(image_slice - expected_slice).max() < 5e-3


@nightly
@require_torch_gpu
class RDMPipelineNightlyTests(unittest.TestCase):
    def tearDown(self):
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()

    def get_inputs(self, device, generator_device="cpu", dtype=torch.float32, seed=0):
        generator = torch.Generator(device=generator_device).manual_seed(seed)
        latents = np.random.RandomState(seed).standard_normal((1, 4, 64, 64))
        latents = torch.from_numpy(latents).to(device=device, dtype=dtype)
        inputs = {
            "prompt": "a photograph of an astronaut riding a horse",
            "latents": latents,
            "generator": generator,
            "num_inference_steps": 50,
            "guidance_scale": 7.5,
            "output_type": "numpy",
        }
        return inputs

    def test_rdm_pndm(self):
        sd_pipe = RDMPipeline.from_pretrained("fusing/rdm").to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images[0]

        expected_image = load_numpy(
            "https://huggingface.co/datasets/diffusers/test-arrays/resolve/main"
            "/stable_diffusion_text2img/stable_diffusion_pndm.npy"
        )
        max_diff = np.abs(expected_image - image).max()
        assert max_diff < 1e-3

    def test_rdm_ddim(self):
        sd_pipe = RDMPipeline.from_pretrained("fusing/rdm").to(torch_device)
        sd_pipe.scheduler = DDIMScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images[0]

        expected_image = load_numpy(
            "https://huggingface.co/datasets/diffusers/test-arrays/resolve/main"
            "/stable_diffusion_text2img/stable_diffusion_ddim.npy"
        )
        max_diff = np.abs(expected_image - image).max()
        assert max_diff < 1e-3

    def test_rdm_lms(self):
        sd_pipe = RDMPipeline.from_pretrained("fusing/rdm").to(torch_device)
        sd_pipe.scheduler = LMSDiscreteScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images[0]

        expected_image = load_numpy(
            "https://huggingface.co/datasets/diffusers/test-arrays/resolve/main"
            "/stable_diffusion_text2img/stable_diffusion_lms.npy"
        )
        max_diff = np.abs(expected_image - image).max()
        assert max_diff < 1e-3

    def test_rdm_euler(self):
        sd_pipe = RDMPipeline.from_pretrained("fusing/rdm").to(torch_device)
        sd_pipe.scheduler = EulerDiscreteScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images[0]

        expected_image = load_numpy(
            "https://huggingface.co/datasets/diffusers/test-arrays/resolve/main"
            "/stable_diffusion_text2img/stable_diffusion_euler.npy"
        )
        max_diff = np.abs(expected_image - image).max()
        assert max_diff < 1e-3

    def test_rdm_dpm(self):
        sd_pipe = RDMPipeline.from_pretrained("fusing/rdm").to(torch_device)
        sd_pipe.scheduler = DPMSolverMultistepScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        inputs["num_inference_steps"] = 25
        image = sd_pipe(**inputs).images[0]

        expected_image = load_numpy(
            "https://huggingface.co/datasets/diffusers/test-arrays/resolve/main"
            "/stable_diffusion_text2img/stable_diffusion_dpm_multi.npy"
        )
        max_diff = np.abs(expected_image - image).max()
        assert max_diff < 1e-3
