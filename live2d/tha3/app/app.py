import argparse
import ast
import os
import random
import sys
import threading
import time
import torch
import io
import torch.nn.functional as F
import wx
import numpy as np

from PIL import Image
from torchvision import transforms
from flask import Flask, Response
from flask_cors import CORS
from io import BytesIO

sys.path.append(os.getcwd())
from tha3.mocap.ifacialmocap_constants import *
from tha3.mocap.ifacialmocap_pose import create_default_ifacialmocap_pose
from tha3.mocap.ifacialmocap_pose_converter import IFacialMocapPoseConverter
from tha3.mocap.ifacialmocap_poser_converter_25 import create_ifacialmocap_pose_converter
from tha3.poser.modes.load_poser import load_poser
from tha3.poser.poser import Poser
from tha3.util import (
    torch_linear_to_srgb, resize_PIL_image, extract_PIL_image_from_filelike,
    extract_pytorch_image_from_PIL_image
)
from typing import Optional

# Global Variables
global_source_image = None
global_result_image = None
global_reload = None
is_talking_override = False
is_talking = False
global_timer_paused = False
emotion = "joy"
fps = 0

# Flask setup
app = Flask(__name__)
CORS(app)

def setEmotion(_emotion):
    global emotion

    highest_score = float('-inf')
    highest_label = None

    for item in _emotion:
        if item['score'] > highest_score:
            highest_score = item['score']
            highest_label = item['label']

    emotion = highest_label

def unload():
    global global_timer_paused
    global_timer_paused = True
    return "Animation Paused"

def start_talking():
    global is_talking_override
    is_talking_override = True
    return "started"

def stop_talking():
    global is_talking_override
    is_talking_override = False
    return "stopped"

def result_feed():
    def generate():
        while True:
            if global_result_image is not None:
                try:
                    rgb_image = global_result_image[:, :, [2, 1, 0]]  # Swap B and R channels
                    pil_image = Image.fromarray(np.uint8(rgb_image))  # Convert to PIL Image
                    if global_result_image.shape[2] == 4: # Check if there is an alpha channel present
                        alpha_channel = global_result_image[:, :, 3] # Extract alpha channel
                        pil_image.putalpha(Image.fromarray(np.uint8(alpha_channel))) # Set alpha channel in the PIL Image
                    buffer = io.BytesIO() # Save as PNG with RGBA mode
                    pil_image.save(buffer, format='PNG')
                    image_bytes = buffer.getvalue()
                except Exception as e:
                    print(f"Error when trying to write image: {e}")
                yield (b'--frame\r\n'  # Send the PNG image
                       b'Content-Type: image/png\r\n\r\n' + image_bytes + b'\r\n')
            else:
                time.sleep(0.1)
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

def live2d_load_file(stream):
    global global_source_image
    global global_reload
    global global_timer_paused
    global_timer_paused = False
    try:
        pil_image = Image.open(stream) # Load the image using PIL.Image.open
        img_data = BytesIO() # Create a copy of the image data in memory using BytesIO
        pil_image.save(img_data, format='PNG')
        global_reload = Image.open(BytesIO(img_data.getvalue())) # Set the global_reload to the copy of the image data
    except Image.UnidentifiedImageError:
        print(f"Could not load image from file, loading blank")
        full_path = os.path.join(os.getcwd(), "live2d\\tha3\\images\\inital.png")
        MainFrame.load_image(None, full_path)
        global_timer_paused = True
    return 'OK'

def convert_linear_to_srgb(image: torch.Tensor) -> torch.Tensor:
    rgb_image = torch_linear_to_srgb(image[0:3, :, :])
    return torch.cat([rgb_image, image[3:4, :, :]], dim=0)

def launch_gui(device, model):
    global initAMI
    initAMI = True
    
    parser = argparse.ArgumentParser(description='uWu Waifu')

    # Add other parser arguments here

    args, unknown = parser.parse_known_args()

    try:
        poser = load_poser(model, device)
        pose_converter = create_ifacialmocap_pose_converter()

        app = wx.App(redirect=False)
        main_frame = MainFrame(poser, pose_converter, device)
        main_frame.SetSize((750, 600))

        #Lload default image (you can pass args.char if required)
        full_path = os.path.join(os.getcwd(), "live2d\\tha3\\images\\inital.png")
        main_frame.load_image(None, full_path)

        #main_frame.Show(True)
        main_frame.capture_timer.Start(100)
        main_frame.animation_timer.Start(100)
        wx.DisableAsserts() #prevent popup about debug alert closed from other threads
        app.MainLoop()

    except RuntimeError as e:
        print(e)
        sys.exit()

class FpsStatistics:
    def __init__(self):
        self.count = 100
        self.fps = []

    def add_fps(self, fps):
        self.fps.append(fps)
        while len(self.fps) > self.count:
            del self.fps[0]

    def get_average_fps(self):
        if len(self.fps) == 0:
            return 0.0
        else:
            return sum(self.fps) / len(self.fps)

class MainFrame(wx.Frame):
    def __init__(self, poser: Poser, pose_converter: IFacialMocapPoseConverter, device: torch.device):
        super().__init__(None, wx.ID_ANY, "uWu Waifu")
        self.pose_converter = pose_converter
        self.poser = poser
        self.device = device

        self.fps_statistics = FpsStatistics()
        self.image_load_counter = 0
        self.custom_background_image = None  # Add this line

        self.sliders = {}
        self.ifacialmocap_pose = create_default_ifacialmocap_pose()
        self.source_image_bitmap = wx.Bitmap(self.poser.get_image_size(), self.poser.get_image_size())
        self.result_image_bitmap = wx.Bitmap(self.poser.get_image_size(), self.poser.get_image_size())
        self.wx_source_image = None
        self.torch_source_image = None
        self.last_pose = None
        self.last_update_time = None

        self.create_ui()

        self.create_timers()
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self.update_source_image_bitmap()
        self.update_result_image_bitmap()

    def create_timers(self):
        self.capture_timer = wx.Timer(self, wx.ID_ANY)
        self.Bind(wx.EVT_TIMER, self.update_capture_panel, id=self.capture_timer.GetId())
        self.animation_timer = wx.Timer(self, wx.ID_ANY)
        self.Bind(wx.EVT_TIMER, self.update_result_image_bitmap, id=self.animation_timer.GetId())

    def on_close(self, event: wx.Event):
        # Stop the timers
        self.animation_timer.Stop()
        self.capture_timer.Stop()

        # Destroy the windows
        self.Destroy()
        event.Skip()
        sys.exit(0)

    def random_generate_value(self, min, max, origin_value):
        random_value = random.choice(list(range(min, max, 1))) / 2500.0
        randomized = origin_value + random_value
        if randomized > 1.0:
            randomized = 1.0
        if randomized < 0:
            randomized = 0
        return randomized

    def animationTalking(self):
        global is_talking
        current_pose = self.ifacialmocap_pose

        # NOTE: randomize mouth
        for blendshape_name in BLENDSHAPE_NAMES:
            if "jawOpen" in blendshape_name:
                if is_talking or is_talking_override:
                    current_pose[blendshape_name] = self.random_generate_value(-5000, 5000, abs(1 - current_pose[blendshape_name]))
                else:
                    current_pose[blendshape_name] = 0

        return current_pose
    
    def animationHeadMove(self):
        current_pose = self.ifacialmocap_pose

        for key in [HEAD_BONE_Y]: #can add more to this list if needed
            current_pose[key] = self.random_generate_value(-20, 20, current_pose[key])
        
        return current_pose
    
    def animationBlink(self):
        current_pose = self.ifacialmocap_pose

        if random.random() <= 0.03:
            current_pose["eyeBlinkRight"] = 1
            current_pose["eyeBlinkLeft"] = 1
        else:
            current_pose["eyeBlinkRight"] = 0
            current_pose["eyeBlinkLeft"] = 0

        return current_pose
    
    def addNamestoConvert(pose):
        index_to_name = {
            0: 'unknown',
            1: 'unknown',
            2: 'eyebrow_angry_left_index',
            3: 'eyebrow_angry_right_index',
            4: 'unknown',
            5: 'unknown',
            6: 'eyebrow_raised_left_index',
            7: 'eyebrow_raised_right_index',
            8: 'eyebrow_happy_left_index',
            9: 'eyebrow_happy_right_index',
            10: 'unknown',
            11: 'unknown',
            12: 'wink_left_index',
            13: 'wink_right_index',
            14: 'eye_happy_wink_left_index',
            15: 'eye_happy_wink_right_index',
            16: 'eye_surprised_left_index',
            17: 'eye_surprised_right_index',
            18: 'unknown',
            19: 'unknown',
            20: 'unknown',
            21: 'unknown',
            22: 'eye_raised_lower_eyelid_left_index',
            23: 'eye_raised_lower_eyelid_right_index',
            24: 'iris_small_left_index',
            25: 'iris_small_right_index',
            26: 'mouth_aaa_index',
            27: 'mouth_iii_index',
            28: 'mouth_ooo_index',
            29: 'unknown',
            30: 'mouth_ooo_index',
            31: 'unknown',
            32: 'unknown',
            33: 'unknown',
            34: 'mouth_raised_corner_left_index',
            35: 'mouth_raised_corner_right_index',
            36: 'unknown',
            37: 'iris_rotation_x_index',
            38: 'iris_rotation_y_index',
            39: 'head_x_index',
            40: 'head_y_index',
            41: 'neck_z_index',
            42: 'body_y_index',
            43: 'body_z_index',
            44: 'breathing_index'
        }

        output = []

        for index, value in enumerate(pose):
            name = index_to_name.get(index, "Unknown")
            output.append(f"{name}: {value}")

        return output
    
    def get_emotion_values(self, emotion): # Place to define emotion presets
        emotions = {
            'admiration': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.4, 'eye_relaxed_right_index': 0.4, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.6, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.3, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'amusement': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.6, 'eyebrow_happy_right_index': 0.6, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.4, 'eye_happy_wink_right_index': 0.4, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.8, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.5, 'mouth_raised_corner_right_index': 0.5, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'anger': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 1.0, 'eyebrow_angry_right_index': 1.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'annoyance': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 1.0, 'eyebrow_angry_right_index': 1.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'approval': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.6, 'mouth_raised_corner_right_index': 0.6, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'caring': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.5, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.6, 'mouth_raised_corner_right_index': 0.6, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'confusion': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 1.0, 'eye_relaxed_right_index': 1.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.2, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'curiosity': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 1.0, 'eyebrow_raised_right_index': 1.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 1.0, 'eye_relaxed_right_index': 1.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.2, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'desire': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 1.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'disappointment': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.5, 'eyebrow_serious_right_index': 0.5, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.5, 'mouth_lowered_corner_right_index': 0.5, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'disapproval': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.5, 'eyebrow_angry_right_index': 0.5, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'disgust': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.5, 'eyebrow_angry_right_index': 0.5, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 1.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.5, 'mouth_lowered_corner_right_index': 0.5, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'embarrassment': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'excitement': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 1.0, 'eyebrow_raised_right_index': 1.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'fear': {'eyebrow_troubled_left_index': 1.0, 'eyebrow_troubled_right_index': 1.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 1.0, 'iris_small_right_index': 1.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'gratitude': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 1.0, 'eye_relaxed_right_index': 1.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.5, 'mouth_raised_corner_right_index': 0.5, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'grief': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'joy': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 1.0, 'eyebrow_happy_right_index': 1.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 1.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 1.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.5, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 1.0, 'mouth_raised_corner_right_index': 1.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'love': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.5, 'eyebrow_happy_right_index': 0.5, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.5, 'eye_relaxed_right_index': 0.5, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.5, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.5, 'mouth_raised_corner_right_index': 0.5, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'nervousness': {'eyebrow_troubled_left_index': 0.5, 'eyebrow_troubled_right_index': 0.5, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.5, 'eye_raised_lower_eyelid_right_index': 0.5, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 1.2, 'mouth_raised_corner_right_index': 1.2, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 1.0},
            'neutral': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 1.0, 'eyebrow_raised_right_index': 1.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.5, 'eye_relaxed_right_index': 0.5, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.5, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'optimism': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.5, 'eye_relaxed_right_index': 0.5, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.5, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'pride': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.5, 'eyebrow_serious_right_index': 0.5, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 1.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.5, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.5, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.5, 'mouth_raised_corner_right_index': 0.5, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'realization': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 1.0, 'eyebrow_raised_right_index': 1.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.5, 'eye_relaxed_right_index': 0.5, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.5, 'mouth_lowered_corner_left_index': -0.2, 'mouth_lowered_corner_right_index': -0.2, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'relief': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.5, 'eyebrow_happy_right_index': 0.5, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 1.0, 'eye_relaxed_right_index': 1.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.6, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.5, 'mouth_raised_corner_right_index': 0.5, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'remorse': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': -0.2, 'mouth_lowered_corner_right_index': -0.2, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'sadness': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 0.0, 'eyebrow_lowered_left_index': 0.1, 'eyebrow_lowered_right_index': 0.1, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': -0.1, 'mouth_lowered_corner_right_index': -0.1, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'surprise': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 1.0, 'eyebrow_raised_right_index': 1.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 1.0, 'eye_surprised_right_index': 1.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.8, 'iris_small_right_index': 0.8, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.5, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0},
            'zero': {'eyebrow_troubled_left_index': 0.0, 'eyebrow_troubled_right_index': 0.0, 'eyebrow_angry_left_index': 0.0, 'eyebrow_angry_right_index': 0.0, 'eyebrow_happy_left_index': 0.0, 'eyebrow_happy_right_index': 0.0, 'eyebrow_raised_left_index': 0.0, 'eyebrow_raised_right_index': 1.0, 'eyebrow_lowered_left_index': 0.0, 'eyebrow_lowered_right_index': 0.0, 'eyebrow_serious_left_index': 0.0, 'eyebrow_serious_right_index': 0.0, 'eye_surprised_left_index': 0.0, 'eye_surprised_right_index': 0.0, 'eye_wink_left_index': 0.0, 'eye_wink_right_index': 0.0, 'eye_happy_wink_left_index': 0.0, 'eye_happy_wink_right_index': 0.0, 'eye_relaxed_left_index': 0.0, 'eye_relaxed_right_index': 0.0, 'eye_raised_lower_eyelid_left_index': 0.0, 'eye_raised_lower_eyelid_right_index': 0.0, 'iris_small_left_index': 0.0, 'iris_small_right_index': 0.0, 'iris_rotation_x_index': 0.0, 'iris_rotation_y_index': 0.0, 'head_x_index': 0.0, 'head_y_index': 0.0, 'neck_z_index': 0.0, 'mouth_aaa_index': 0.0, 'mouth_iii_index': 0.0, 'mouth_uuu_index': 0.0, 'mouth_eee_index': 0.0, 'mouth_ooo_index': 0.0, 'mouth_lowered_corner_left_index': 0.0, 'mouth_lowered_corner_right_index': 0.0, 'mouth_raised_corner_left_index': 0.0, 'mouth_raised_corner_right_index': 0.0, 'body_y_index': 0.0, 'body_z_index': 0.0, 'breathing_index': 0.0}
        }
        
        targetpose = emotions.get(emotion, {})
        #print("targetpose ", targetpose)

        targetpose_values = list(targetpose.values())
        #print("targetpose_values ", targetpose_values)
        return targetpose_values

    def animateToEmotion(self, current_pose_list, target_pose_str):
        current_pose = {part.split(": ")[0]: float(part.split(": ")[1]) for part in current_pose_list if part.split(": ")[0] != "unknown"}

        new_pose = {}

        for part, current_value in current_pose.items():
            if part in target_pose_str:
                target_value = target_pose_str[part]
                if current_value < target_value:
                    new_value = min(current_value + 0.10, target_value)
                elif current_value > target_value:
                    new_value = max(current_value - 0.10, target_value)
                else:
                    new_value = current_value
                new_pose[part] = new_value

        return new_pose
    

    def animationMain(self): 
        self.ifacialmocap_pose =  self.animationBlink()
        self.ifacialmocap_pose =  self.animationHeadMove()
        self.ifacialmocap_pose =  self.animationTalking()
        return self.ifacialmocap_pose

    def on_erase_background(self, event: wx.Event):
        pass

    def create_animation_panel(self, parent):
        self.animation_panel = wx.Panel(parent, style=wx.RAISED_BORDER)
        self.animation_panel_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.animation_panel.SetSizer(self.animation_panel_sizer)
        self.animation_panel.SetAutoLayout(1)

        image_size = self.poser.get_image_size()

        # Left Column (Image)
        self.animation_left_panel = wx.Panel(self.animation_panel, style=wx.SIMPLE_BORDER)
        self.animation_left_panel_sizer = wx.BoxSizer(wx.VERTICAL)
        self.animation_left_panel.SetSizer(self.animation_left_panel_sizer)
        self.animation_left_panel.SetAutoLayout(1)
        self.animation_panel_sizer.Add(self.animation_left_panel, 1, wx.EXPAND)

        self.result_image_panel = wx.Panel(self.animation_left_panel, size=(image_size, image_size),
                                           style=wx.SIMPLE_BORDER)
        self.result_image_panel.Bind(wx.EVT_PAINT, self.paint_result_image_panel)
        self.result_image_panel.Bind(wx.EVT_ERASE_BACKGROUND, self.on_erase_background)
        self.result_image_panel.Bind(wx.EVT_LEFT_DOWN, self.load_image)
        self.animation_left_panel_sizer.Add(self.result_image_panel, 1, wx.EXPAND)

        separator = wx.StaticLine(self.animation_left_panel, -1, size=(256, 1))
        self.animation_left_panel_sizer.Add(separator, 0, wx.EXPAND)
        
        self.fps_text = wx.StaticText(self.animation_left_panel, label="")
        self.animation_left_panel_sizer.Add(self.fps_text, wx.SizerFlags().Border())

        self.animation_left_panel_sizer.Fit(self.animation_left_panel)

        # Right Column (Sliders)

        self.animation_right_panel = wx.Panel(self.animation_panel, style=wx.SIMPLE_BORDER)
        self.animation_right_panel_sizer = wx.BoxSizer(wx.VERTICAL)
        self.animation_right_panel.SetSizer(self.animation_right_panel_sizer)
        self.animation_right_panel.SetAutoLayout(1)
        self.animation_panel_sizer.Add(self.animation_right_panel, 1, wx.EXPAND)

        separator = wx.StaticLine(self.animation_right_panel, -1, size=(256, 5))
        self.animation_right_panel_sizer.Add(separator, 0, wx.EXPAND)

        background_text = wx.StaticText(self.animation_right_panel, label="--- Background ---", style=wx.ALIGN_CENTER)
        self.animation_right_panel_sizer.Add(background_text, 0, wx.EXPAND)

        self.output_background_choice = wx.Choice(
            self.animation_right_panel,
            choices=[
                "TRANSPARENT",
                "GREEN",
                "BLUE",
                "BLACK",
                "WHITE",
                "LOADED",
                "CUSTOM"
            ]
        )
        self.output_background_choice.SetSelection(0)
        self.animation_right_panel_sizer.Add(self.output_background_choice, 0, wx.EXPAND)




        blendshape_groups = {
            'Eyes': ['eyeLookOutLeft', 'eyeLookOutRight', 'eyeLookDownLeft', 'eyeLookUpLeft', 'eyeWideLeft', 'eyeWideRight'],
            'Mouth': ['mouthFrownLeft'],
            'Cheek': ['cheekSquintLeft', 'cheekSquintRight', 'cheekPuff'],
            'Brow': ['browDownLeft', 'browOuterUpLeft', 'browDownRight', 'browOuterUpRight', 'browInnerUp'],
            'Eyelash': ['mouthSmileLeft'],
            'Nose': ['noseSneerLeft', 'noseSneerRight'],
            'Misc': ['tongueOut']
        }

        for group_name, variables in blendshape_groups.items():
            collapsible_pane = wx.CollapsiblePane(self.animation_right_panel, label=group_name, style=wx.CP_DEFAULT_STYLE | wx.CP_NO_TLW_RESIZE)
            collapsible_pane.Bind(wx.EVT_COLLAPSIBLEPANE_CHANGED, self.on_pane_changed)
            self.animation_right_panel_sizer.Add(collapsible_pane, 0, wx.EXPAND)
            pane_sizer = wx.BoxSizer(wx.VERTICAL)
            collapsible_pane.GetPane().SetSizer(pane_sizer)

            for variable in variables:
                variable_label = wx.StaticText(collapsible_pane.GetPane(), label=variable)

                # Multiply min and max values by 100 for the slider
                slider = wx.Slider(
                    collapsible_pane.GetPane(),
                    value=0,
                    minValue=0,
                    maxValue=100,
                    size=(150, -1),  # Set the width to 150 and height to default
                    style=wx.SL_HORIZONTAL | wx.SL_LABELS
                )

                slider.SetName(variable)
                slider.Bind(wx.EVT_SLIDER, self.on_slider_change)
                self.sliders[slider.GetId()] = slider

                pane_sizer.Add(variable_label, 0, wx.ALIGN_CENTER | wx.ALL, 5)
                pane_sizer.Add(slider, 0, wx.EXPAND)

        self.animation_right_panel_sizer.Fit(self.animation_right_panel)
        self.animation_panel_sizer.Fit(self.animation_panel)

    def on_pane_changed(self, event):
        # Update the layout when a collapsible pane is expanded or collapsed
        self.animation_right_panel.Layout()

    def on_slider_change(self, event):
        slider = event.GetEventObject()
        value = slider.GetValue() / 100.0  # Divide by 100 to get the actual float value
        #print(value)
        slider_name = slider.GetName()
        self.ifacialmocap_pose[slider_name] = value

    def create_ui(self):
        #MAke the UI Elements
        self.main_sizer = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self.main_sizer)
        self.SetAutoLayout(1)

        self.capture_pose_lock = threading.Lock()

        #Main panel with JPS
        self.create_animation_panel(self)
        self.main_sizer.Add(self.animation_panel, wx.SizerFlags(0).Expand().Border(wx.ALL, 5))

    def update_capture_panel(self, event: wx.Event):
        data = self.ifacialmocap_pose
        for rotation_name in ROTATION_NAMES:
            value = data[rotation_name]

    @staticmethod
    def convert_to_100(x):
        return int(max(0.0, min(1.0, x)) * 100)

    def paint_source_image_panel(self, event: wx.Event):
        wx.BufferedPaintDC(self.source_image_panel, self.source_image_bitmap)

    def update_source_image_bitmap(self):
        dc = wx.MemoryDC()
        dc.SelectObject(self.source_image_bitmap)
        if self.wx_source_image is None:
            self.draw_nothing_yet_string(dc)
        else:
            dc.Clear()
            dc.DrawBitmap(self.wx_source_image, 0, 0, True)
        del dc

    def draw_nothing_yet_string(self, dc):
        dc.Clear()
        font = wx.Font(wx.FontInfo(14).Family(wx.FONTFAMILY_SWISS))
        dc.SetFont(font)
        w, h = dc.GetTextExtent("Nothing yet!")
        dc.DrawText("Nothing yet!", (self.poser.get_image_size() - w) // 2, (self.poser.get_image_size() - h) // 2)

    def paint_result_image_panel(self, event: wx.Event):
        wx.BufferedPaintDC(self.result_image_panel, self.result_image_bitmap)

    def combine_pose_with_names(combine_pose):
        pose_names = [
            'eyeLookInLeft', 'eyeLookOutLeft', 'eyeLookDownLeft', 'eyeLookUpLeft',
            'eyeBlinkLeft', 'eyeSquintLeft', 'eyeWideLeft', 'eyeLookInRight',
            'eyeLookOutRight', 'eyeLookDownRight', 'eyeLookUpRight', 'eyeBlinkRight',
            'eyeSquintRight', 'eyeWideRight', 'browDownLeft', 'browOuterUpLeft',
            'browDownRight', 'browOuterUpRight', 'browInnerUp', 'noseSneerLeft',
            'noseSneerRight', 'cheekSquintLeft', 'cheekSquintRight', 'cheekPuff',
            'mouthLeft', 'mouthDimpleLeft', 'mouthFrownLeft', 'mouthLowerDownLeft',
            'mouthPressLeft', 'mouthSmileLeft', 'mouthStretchLeft', 'mouthUpperUpLeft',
            'mouthRight', 'mouthDimpleRight', 'mouthFrownRight', 'mouthLowerDownRight',
            'mouthPressRight', 'mouthSmileRight', 'mouthStretchRight', 'mouthUpperUpRight',
            'mouthClose', 'mouthFunnel', 'mouthPucker', 'mouthRollLower', 'mouthRollUpper',
            'mouthShrugLower', 'mouthShrugUpper', 'jawLeft', 'jawRight', 'jawForward',
            'jawOpen', 'tongueOut', 'headBoneX', 'headBoneY', 'headBoneZ', 'headBoneQuat',
            'leftEyeBoneX', 'leftEyeBoneY', 'leftEyeBoneZ', 'leftEyeBoneQuat',
            'rightEyeBoneX', 'rightEyeBoneY', 'rightEyeBoneZ', 'rightEyeBoneQuat'
        ]
        pose_dict = dict(zip(pose_names, combine_pose))
        return pose_dict

    def update_result_image_bitmap(self, event: Optional[wx.Event] = None):
        global global_timer_paused
        global initAMI
        global global_result_image
        global global_reload
        global emotion
        global fps

        if global_timer_paused:
            return

        try:
            if global_reload is not None:
                MainFrame.load_image(self, event=None, file_path=None)  # call load_image function here
                return
            
            ################ START ANIMATION CHANGES ##########################

            ifacialmocap_pose = self.animationMain() #GET ANIMATION CHANGES

            #print("-------------------------------------------")
            current_pose = self.pose_converter.convert(ifacialmocap_pose)
            #print("current_pose ", current_pose)

            ############### ADD BREATHING TALKING HEAD MOVEMENTS HERE ########
            #print("-------------------------------------------")
            #mod_pose = self.pose_converter.convert(ifacialmocap_pose)
            #print("mod_pose ", mod_pose)


            #print("-------------------------------------------")
            #adjusted_pose = self.get_emotion_values(emotion) #Set emotion tage
            #print("adjusted_pose ", adjusted_pose)

            #print("-------------------------------------------")
            #n = len(adjusted_pose)
            #combined_pose = adjusted_pose + current_pose[n:]
            #print("combined_pose ", combined_pose)

            ################ END ANIMATION CHANGES ##########################

            combined_pose = current_pose

            if self.last_pose is not None and self.last_pose == combined_pose:
                return
            
            self.last_pose = combined_pose

            if self.torch_source_image is None:
                dc = wx.MemoryDC()
                dc.SelectObject(self.result_image_bitmap)
                self.draw_nothing_yet_string(dc)
                del dc
                return

            pose = torch.tensor(combined_pose, device=self.device, dtype=self.poser.get_dtype())

            with torch.no_grad():
                output_image = self.poser.pose(self.torch_source_image, pose)[0].float()
                output_image = convert_linear_to_srgb((output_image + 1.0) / 2.0)

                c, h, w = output_image.shape
                output_image = (255.0 * torch.transpose(output_image.reshape(c, h * w), 0, 1)).reshape(h, w, c).byte()


            numpy_image = output_image.detach().cpu().numpy()
            wx_image = wx.ImageFromBuffer(numpy_image.shape[0],
                                        numpy_image.shape[1],
                                        numpy_image[:, :, 0:3].tobytes(),
                                        numpy_image[:, :, 3].tobytes())
            wx_bitmap = wx_image.ConvertToBitmap()

            dc = wx.MemoryDC()
            dc.SelectObject(self.result_image_bitmap)
            dc.Clear()
            dc.DrawBitmap(wx_bitmap,
                        (self.poser.get_image_size() - numpy_image.shape[0]) // 2,
                        (self.poser.get_image_size() - numpy_image.shape[1]) // 2, True)

            numpy_image_bgra = numpy_image[:, :, [2, 1, 0, 3]] # Convert color channels from RGB to BGR and keep alpha channel
            global_result_image = numpy_image_bgra

            del dc


            time_now = time.time_ns()
            if self.last_update_time is not None:
                elapsed_time = time_now - self.last_update_time
                fps = 1.0 / (elapsed_time / 10**9)

                if self.torch_source_image is not None:
                    self.fps_statistics.add_fps(fps)
                self.fps_text.SetLabelText("FPS = %0.2f" % self.fps_statistics.get_average_fps())

            self.last_update_time = time_now

            if(initAMI == True): #If the models are just now initalized stop animation to save
                global_timer_paused = True
                initAMI = False

            if random.random() <= 0.01:
                trimmed_fps = round(fps, 1)
                print("Current FPS: {:.1f}".format(trimmed_fps))

            self.Refresh()

        except KeyboardInterrupt:
            print("Update process was interrupted by the user.")
            wx.Exit()
    


    def resize_image(image, size=(512, 512)):
        image.thumbnail(size, Image.LANCZOS)  # Step 1: Resize the image to maintain the aspect ratio with the larger dimension being 512 pixels
        new_image = Image.new("RGBA", size)   # Step 2: Create a new image of size 512x512 with transparency
        new_image.paste(image, ((size[0] - image.size[0]) // 2,
                                (size[1] - image.size[1]) // 2))   # Step 3: Paste the resized image into the new image, centered
        return new_image

    def load_image(self, event: wx.Event, file_path=None):

        global global_source_image  # Declare global_source_image as a global variable
        global global_reload

        if global_reload is not None:
            file_path = "global_reload"

        try:   
            if file_path == "global_reload":
                pil_image = global_reload 
            else:
                pil_image = resize_PIL_image(
                    extract_PIL_image_from_filelike(file_path),
                    (self.poser.get_image_size(), self.poser.get_image_size()))

            w, h = pil_image.size

            if pil_image.size != (512, 512):
                print("Resizing Char Card to work")
                pil_image = MainFrame.resize_image(pil_image)

            w, h = pil_image.size

            if pil_image.mode != 'RGBA':
                self.source_image_string = "Image must have alpha channel!"
                self.wx_source_image = None
                self.torch_source_image = None
            else:
                self.wx_source_image = wx.Bitmap.FromBufferRGBA(w, h, pil_image.convert("RGBA").tobytes())
                self.torch_source_image = extract_pytorch_image_from_PIL_image(pil_image) \
                    .to(self.device).to(self.poser.get_dtype())

            global_source_image = self.torch_source_image  # Set global_source_image as a global variable

            self.update_source_image_bitmap()

        except Exception as error:
            print("Error: ", error)

        global_reload = None #reset the globe load
        self.Refresh()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='uWu Waifu')
    parser.add_argument(
        '--model',
        type=str,
        required=False,
        default='separable_float',
        choices=['standard_float', 'separable_float', 'standard_half', 'separable_half'],
        help='The model to use.'
    )
    parser.add_argument('--char', type=str, required=False, help='The path to the character image.')
    parser.add_argument(
        '--device',
        type=str,
        required=False,
        default='cuda',
        choices=['cpu', 'cuda'],
        help='The device to use for PyTorch ("cuda" for GPU, "cpu" for CPU).'
    )

    args = parser.parse_args()
    launch_gui(device=args.device, model=args.model)