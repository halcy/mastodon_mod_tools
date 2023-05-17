import torch
from PIL import Image
import open_clip
from glob import glob
import json
from pathlib import Path
import copy
from collections import defaultdict, OrderedDict
import requests
import io
import time
import pickle
import os
import sys
from mastodon import Mastodon
import numpy as np

# Settings
app_settings = {
    "credential_file": "/home/halcy/masto/pytooter_usercred_ADMIN_DANGER.secret",
    "raw_db_dir": "/mnt/c/Users/halcy/Desktop/Automod/db_raw/",
    "embed_db_file": "/mnt/c/Users/halcy/Desktop/Automod/db.pkl",
    "image_extensions": ["gif", "png", "jpg", "jpeg"],
    "wait_time": 120,
    "preemptive_silence": False,
    "panic_stop": 10,
}

# Empty trigger database for initial state
trigger_db = {
    "embeds": defaultdict(OrderedDict),
    "pre_matrices": { },
    "config": None,
    "last_checked_user_id": 0,
}

# Embed helpers
def get_text_embed(text, tokenizer, clip_model):
    with torch.no_grad():
        text = text_tokenizer(text)
        text_embed = clip_model.encode_text(text)
        text_embed /= text_embed.norm(dim=-1, keepdim=True)
        text_embed = text_embed[0].cpu().numpy()
    return text_embed

def get_image_embed(image, image_preprocessor, clip_model):
    with torch.no_grad():
        image = image_preprocessor(image).unsqueeze(0)
        image_embed = clip_model.encode_image(image)
        image_embed /= image_embed.norm(dim=-1, keepdim=True)
        image_embed = image_embed[0].cpu().numpy()
    return image_embed

# IO helpers
def read_image(path):
    return Image.open(path).convert("RGB")

def glob_multiple(path, extensions):
    files = []
    for extension in extensions:
        files += list(glob(str(Path(path) / f"*.{extension}")))
    return files

def read_image_online(url):
    try:
        response = requests.get(url)
        image_file = io.BytesIO(response.content)
        return Image.open(image_file).convert("RGB")
    except:
        return None

# Update the embed cache database
def update_db(base_db, app_settings, models):
    # Working copy
    trigger_db_updated = copy.deepcopy(base_db)
    
    # Update classifier config
    trigger_db_updated["config"] = json.load(open(Path(app_settings["raw_db_dir"]) / "config.json", 'rb'))    
    
    # Update embeds
    for field, field_data in trigger_db_updated["config"]["fields"].items():
        dirty = False
        if field_data["type"] == "image":
            images = glob_multiple(Path(app_settings["raw_db_dir"]) / field, app_settings["image_extensions"])
            for image in images:
                name = Path(image).name
                if not name in trigger_db_updated["embeds"][field]:
                    dirty = True
                    image_data = read_image(image)
                    trigger_db_updated["embeds"][field][name] = get_image_embed(image_data, models["image_preprocessor"], models["clip_model"])

        if field_data["type"] == "text":                    
            field_texts = json.load(open(Path(app_settings["raw_db_dir"]) / (field + ".json"), 'rb'))
            for text in field_texts:
                if not text in trigger_db_updated["embeds"][field]:
                    dirty = True
                    trigger_db_updated["embeds"][field][text] = get_text_embed(text, models["text_tokenizer"], models["clip_model"]) 
                    
        if dirty:
            trigger_db_updated["pre_matrices"][field] = np.vstack(list(trigger_db_updated["embeds"][field].values()))
    
    return trigger_db_updated

# Test user against trigger db
def eval_user(user_dict, trigger_db, models):
    matches = []
    best_match_likelihood = 0.0
    
    for field in trigger_db["pre_matrices"]:
        # Find embed value for field
        field_embed = None
        field_text_value = None
        if trigger_db["config"]["fields"][field]["type"] == "image":
            image = read_image_online(user_dict[field])
            if not image is None:
                field_embed = get_image_embed(image, models["image_preprocessor"], models["clip_model"])
        if trigger_db["config"]["fields"][field]["type"] == "text":
            field_embed = get_text_embed(user_dict[field], models["text_tokenizer"], models["clip_model"]) 
        
        # Compare with database
        if not field_embed is None:
            cosine_sim_matrix = trigger_db["pre_matrices"][field] @ field_embed
            field_match_likelihood = np.max(cosine_sim_matrix)
            if field_match_likelihood >= trigger_db["config"]["fields"][field]["threshold"]:
                match_idx = np.argmax(cosine_sim_matrix)
                matches.append([field, field_match_likelihood, user_dict[field], list(trigger_db["embeds"][field].keys())[match_idx]])
            best_match_likelihood = max(best_match_likelihood, field_match_likelihood)
            
    # See if we hit any match conditions
    hit = False
    reason = None
    if best_match_likelihood >= trigger_db["config"]["overall_threshold_likelihood"]:
        hit = True
        reason = "Exceeded overall likelihood threshold."
        
    if len(matches) >= trigger_db["config"]["overall_threshold_flags"]:
        hit = True
        reason = "Exceeded flagged fields threshold."
        
    # Generate response text
    response_text = ""
    if hit:
        response_text = f"Reason: {reason}\n\nMatches:\n"
        for field, likelihood, field_value, matched_value in matches:
            response_text += f" * {field} = '{field_value}' matched db entry '{matched_value}' with likelihood {likelihood}\n"
    return hit, response_text, matches

# Load models
clip_model, _, image_preprocessor = open_clip.create_model_and_transforms('ViT-B-32', pretrained='laion2b_s34b_b79k')
text_tokenizer = open_clip.get_tokenizer('ViT-B-32')
models = {
    "clip_model": clip_model,
    "text_tokenizer": text_tokenizer,
    "image_preprocessor": image_preprocessor,
}

# Set up masto api access
mastodon = Mastodon(access_token = app_settings["credential_file"])

# Load trigger db cache, if we have one
if os.path.exists(app_settings["embed_db_file"]):
    with open(app_settings["embed_db_file"], 'rb') as f:
        trigger_db = pickle.load(f)

# User checker loop        
while True:
    # Update trigger database
    trigger_db = update_db(trigger_db, app_settings, models)

    # Get new users
    if trigger_db["last_checked_user_id"] == 0:
        accounts = mastodon.admin_accounts_v2(origin="remote", status="active")
    else:
        accounts = mastodon.fetch_remaining(mastodon.admin_accounts_v2(origin="remote", status="active", since_id = trigger_db["last_checked_user_id"]))
    if len(accounts) != 0:
        trigger_db["last_checked_user_id"] = np.max([x.id for x in accounts])

    # TODO: Real logging
    print(f"Checking {len(accounts)} new users.")

    # Store trigger db cache
    with open(app_settings["embed_db_file"], 'wb') as f:
        pickle.dump(trigger_db, f, protocol = pickle.HIGHEST_PROTOCOL)

    # Check users
    panic_stop = 0
    for user in accounts:
        account_dict = user.account
        hit, reason, matches = eval_user(account_dict, trigger_db, models)
        if hit:
            # Log hit (TODO: real logging)
            print(f"Hit on user {account_dict.acct}\n\n{reason}")

            # File report
            report = mastodon.report(account_dict, comment=f"/!\ AUTOMATED DETECTION /!\\n\nReason: {reason}")
            panic_stop += 1
            if panic_stop >= app_settings["panic_stop"]:
                print("Panic - reporting users at too great a rate. Exiting.")
                sys.exit(0)

            # If desired: Silence user immediately and leave it for mod to unsilence if false positive
            if app_settings["preemptive_silence"]:
                mastodon.admin_account_moderate(account_dict, action="silence", report = report)
                mastodon.admin_report_reopen(report)

    # Wait until next period
    time.sleep(app_settings["wait_time"])
    