# Imports
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
import threading

"""
First, some utilities that I didn't feel like bothering putting into the class
"""
# Embed helpers
def get_text_embed(text, tokenizer, clip_model):
    with torch.no_grad():
        text = tokenizer(text)
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
    return Image.open(path).convert("RGBA").convert("RGB")

def glob_multiple(path, extensions):
    files = []
    for extension in extensions:
        files += list(glob(str(Path(path) / f"*.{extension}")))
    return files

def read_image_online(url):
    try:
        response = requests.get(url)
        image_file = io.BytesIO(response.content)
        return Image.open(image_file).convert("RGBA").convert("RGB")
    except:
        return None

class Goku:
    """
    It's Goku, the Guarding Online Kommunications Utility.
    """
    def __init__(self, component_manager):
        """
        Component init. Could have multiple, but should really only have one of these.
        """
        self.component_manager = component_manager
        self._is_running = threading.Event()
        self._stop_request = threading.Event()
        self._worker_thread = None

        # Empty trigger database for initial state
        self.trigger_db = {
            "embeds": defaultdict(OrderedDict),
            "pre_matrices": { },
            "config": None,
            "last_checked_user_id": 0,
            "field_history": defaultdict(list),
            "reported_ids": set( ),
            "seen_ids": list( )
        }

        # Load trigger db cache, if we have one
        if os.path.exists(self.component_manager.get_component("settings").get_config("goku")["embed_db_file"]):
            with open(self.component_manager.get_component("settings").get_config("goku")["embed_db_file"], 'rb') as f:
                self.trigger_db = pickle.load(f)

        # Load models
        clip_model, _, image_preprocessor = open_clip.create_model_and_transforms('ViT-B-32', pretrained='laion2b_s34b_b79k')
        text_tokenizer = open_clip.get_tokenizer('ViT-B-32')
        self.models = {
            "clip_model": clip_model,
            "text_tokenizer": text_tokenizer,
            "image_preprocessor": image_preprocessor,
        }

    def start(self):
        """
        Start thread, if not running
        """
        if not self._is_running.is_set():
            self.component_manager.get_component("logging").add_log("Goku", "Info", "Starting component")
            self._stop_request.clear()
            self._is_running.set()
            self._worker_thread = threading.Thread(target=self.user_check_loop, daemon=True)
            self._worker_thread.start()
            
    def stop(self):
        self._stop_request.set()
        self.component_manager.get_component("logging").add_log("Goku", "Info", "Stop requested")
        if self._worker_thread:
            self._worker_thread.join()

    def state(self):
        if not self._is_running.is_set():
            self._stop_request.clear()
        if self._stop_request.is_set():
            return "stop_requested"
        if self._is_running.is_set():
            return "running"
        return "stopped"

    def update_db(self):
        """
        Update the trigger database
        """
        # Working copy
        trigger_db_updated = copy.deepcopy(self.trigger_db)
        
        # Update classifier config
        trigger_db_updated["config"] = json.load(open(Path(self.component_manager.get_component("settings").get_config("goku")["raw_db_dir"]) / "config.json", 'rb'))    
        
        # Update embeds
        for field, field_data in trigger_db_updated["config"]["fields"].items():
            dirty = False
            if field_data["type"] == "image":
                images = glob_multiple(Path(self.component_manager.get_component("settings").get_config("goku")["raw_db_dir"]) / field, self.component_manager.get_component("settings").get_config("goku")["image_extensions"])
                for image in images:
                    name = Path(image).name
                    if not name in trigger_db_updated["embeds"][field]:
                        dirty = True
                        image_data = read_image(image)
                        trigger_db_updated["embeds"][field][name] = get_image_embed(image_data, self.models["image_preprocessor"], self.models["clip_model"])

            if field_data["type"] == "text":                    
                field_texts = json.load(open(Path(self.component_manager.get_component("settings").get_config("goku")["raw_db_dir"]) / (field + ".json"), 'rb'))
                for text in field_texts:
                    if not text in trigger_db_updated["embeds"][field]:
                        dirty = True
                        trigger_db_updated["embeds"][field][text] = get_text_embed(text,self. models["text_tokenizer"], self.models["clip_model"]) 
                        
            if dirty:
                trigger_db_updated["pre_matrices"][field] = np.vstack(list(trigger_db_updated["embeds"][field].values()))

        self.trigger_db = trigger_db_updated

    def eval_user(self, user_dict):
        """
        Test user against trigger db and do similarity check
        Returns a LIST of results, since it can generate multiple reported users
        """
        matches = []
        reports = []
        best_match_likelihood = 0.0
        similarity_match_fields = []
        similarity_match_cross = None

        for field in self.trigger_db["pre_matrices"]:
            # Check against ignore list so we don't report for missing ava/header, or being the internal fetch actor
            if user_dict[field] in self.trigger_db["config"]["fields"][field]["ignore"]:
                continue

            # Bail if below minimum length
            min_len = 1
            if self.trigger_db["config"]["fields"][field]["type"] == "text":
                min_len = self.trigger_db["config"]["fields"][field]["min_len"]
            if len(user_dict[field]) < min_len:
                continue

            # Find embed value for field
            field_embed = None
            if self.trigger_db["config"]["fields"][field]["type"] == "image":
                image = read_image_online(user_dict[field])
                if not image is None:
                    field_embed = get_image_embed(image, self.models["image_preprocessor"], self.models["clip_model"])
            if self.trigger_db["config"]["fields"][field]["type"] == "text":
                field_embed = get_text_embed(user_dict[field], self.models["text_tokenizer"], self.models["clip_model"]) 
            
            # Compare with database
            if not field_embed is None:
                cosine_sim_matrix = self.trigger_db["pre_matrices"][field] @ field_embed
                field_match_likelihood = np.max(cosine_sim_matrix)
                if field_match_likelihood >= self.trigger_db["config"]["fields"][field]["threshold"]:
                    match_idx = np.argmax(cosine_sim_matrix)
                    matches.append([field, field_match_likelihood, user_dict[field], list(self.trigger_db["embeds"][field].keys())[match_idx]])
                best_match_likelihood = max(best_match_likelihood, field_match_likelihood)

                # Compare with history
                if len(self.trigger_db["field_history"][field]) > 0:
                    history_matrix = np.array([x[1] for x in self.trigger_db["field_history"][field]])
                    similarity_matches = ((history_matrix @ field_embed) > self.trigger_db["config"]["fields"][field]["threshold_similar"])
                    if np.sum(similarity_matches) >= self.trigger_db["config"]["similar_users_count_threshold"]:
                        similarity_match_fields.append(field)
                        similarity_match_dict = dict(zip(similarity_matches, [x[0] for x in self.trigger_db["field_history"][field]]))
                        if similarity_match_cross is None:
                            similarity_match_cross = similarity_match_dict
                        else:
                            similarity_match_cross = {x: similarity_match_dict[x] for x in similarity_match_dict.keys() & similarity_match_cross.keys()}

                # Append to history
                self.trigger_db["field_history"][field].append((user_dict, field_embed))
                self.trigger_db["field_history"][field] = self.trigger_db["field_history"][field][-self.trigger_db["config"]["similar_users_history_length"]:]

        # See if we hit any match conditions
        hit = False
        reason = None
        if best_match_likelihood >= self.trigger_db["config"]["overall_threshold_likelihood"]:
            hit = True
            reason = "Exceeded overall likelihood threshold."
            
        if len(matches) >= self.trigger_db["config"]["overall_threshold_flags"]:
            hit = True
            reason = "Exceeded flagged fields threshold."
            
        # And the conditions for similarity match
        if len(similarity_match_fields) >= self.trigger_db["config"]["similar_users_threshold_flags"]:
            # Generate reason string
            reason = f"Similar user count exceeded on fields {similarity_match_fields}. Matching users (matching fields intersection):\n"
            for is_match, match_dict in similarity_match_cross:
                if is_match:
                    reason += f" * {match_dict.acct}, {field} = '{match_dict[field]}'\n"
            reason += f" * {user_dict.acct}, {field} = '{user_dict[field]}'\n"

            # One report for every matching user
            for is_match, match_dict in similarity_match_cross:
                if is_match:
                    reports.append((match_dict, reason))
            reports.append((user_dict, reason))

        # Generate response text
        response_text = ""
        if hit:
            response_text = f"Reason: {reason}\n\nMatches:\n"
            for field, likelihood, field_value, matched_value in matches:
                response_text += f" * {field} = '{field_value}' matched db entry '{matched_value}' with likelihood {likelihood}\n"
            reports.append((user_dict, response_text))
        return reports

    def user_check_loop(self):
        """
        The actual user checker loop
        """
        while not self._stop_request.is_set():
            try:
                # Update trigger database
                self.update_db()

                # Get new users
                accounts = [ ]
                self.component_manager.get_component("logging").add_log("Goku", "Info", f"Fetching next user batch, last seen ID was {self.trigger_db['last_checked_user_id']}")
                fetch_accounts = self.component_manager.get_component("mastodon").admin_accounts_v2(origin="remote", status="active")
                fetched_pages = 1
                should_abort_fetch = False
                while len(fetch_accounts) > 0 and fetched_pages < self.component_manager.get_component("settings").get_config("goku")["max_fetch_pages"]:
                    should_abort_fetch = False
                    for account in fetch_accounts:
                        if account.id in self.trigger_db["seen_ids"]:
                            should_abort_fetch = True
                        else:
                            accounts.append(account)
                            self.trigger_db["seen_ids"].append(account.id)
                            self.trigger_db["seen_ids"] = self.trigger_db["seen_ids"][-self.component_manager.get_component("settings").get_config("goku")["id_hist_length"]:]
                    if self.trigger_db["last_checked_user_id"] == 0:
                        should_abort_fetch = True
                    if should_abort_fetch:
                        break
                    fetched_pages += 1
                    self.component_manager.get_component("logging").add_log("Goku", "Info", f"Fetching page {fetched_pages}")
                    fetch_accounts = self.component_manager.get_component("mastodon").fetch_next(fetch_accounts)
                if len(accounts) != 0:
                    self.trigger_db["last_checked_user_id"] = np.max([x.id for x in accounts])
                self.component_manager.get_component("logging").add_log("Goku", "Info", f"Checking {len(accounts)} new users.")

                # Store trigger db cache
                with open(self.component_manager.get_component("settings").get_config("goku")["embed_db_file"], 'wb') as f:
                    pickle.dump(self.trigger_db, f, protocol = pickle.HIGHEST_PROTOCOL)

                # Check users
                panic_stop = 0
                for user in accounts:
                    account_dict = user.account
                    reports = self.eval_user(account_dict)
                    for report_dict, reason in reports:
                        # Skip already reported
                        if report_dict.id in self.trigger_db["reported_ids"]:
                            continue

                        # Log hit
                        self.component_manager.get_component("logging").add_log("Goku", "Info", f"Hit on user {report_dict.acct}\n\n{reason}")

                        # File report
                        if len(reason) > 950:
                            reason = reason[:950]
                        report = self.component_manager.get_component("mastodon").report(report_dict, comment=f"/!\ AUTOMATED DETECTION /!\n\nReason: {reason}")
                        panic_stop += 1
                        if panic_stop >= self.component_manager.get_component("settings").get_config("goku")["panic_stop"]:
                            self.component_manager.get_component("logging").add_log("Goku", "Info", "Panic - reporting users at too great a rate. Stopping component.")
                            self._stop_request.set()

                        # If desired: Silence user immediately and leave it for mod to unsilence if false positive
                        if self.component_manager.get_component("settings").get_config("goku")["preemptive_silence"] and not self.component_manager.get_component("piccolo").is_closed_regs_instance(report_dict.acct.split("@")[-1]):
                            self.component_manager.get_component("mastodon").admin_account_moderate(report_dict, action="silence", report = report)
                            self.component_manager.get_component("mastodon").admin_report_reopen(report)
                
                        # Add to history
                        self.trigger_db["reported_ids"].add(report_dict.id)

                # Store trigger db cache with updated histories
                with open(self.component_manager.get_component("settings").get_config("goku")["embed_db_file"], 'wb') as f:
                    pickle.dump(self.trigger_db, f, protocol = pickle.HIGHEST_PROTOCOL)

                # Wait until next period
                self.component_manager.get_component("logging").add_log("Goku", "Info", "Entering waiting state")
                wait_time_start = time.time()
                while not self._stop_request.is_set() and time.time() - wait_time_start < self.component_manager.get_component("settings").get_config("goku")["wait_time"]:
                    time.sleep(1.0)
            except Exception as e:
                    _, _, exc_tb = sys.exc_info()
                    self.component_manager.get_component("logging").add_log("Goku", "Error", f"An error occurred in the user check loop: {e} at line {exc_tb.tb_lineno}")
                    time.sleep(1.0)

        self.component_manager.get_component("logging").add_log("Goku", "Info", "Component stopped")
        self._is_running.clear()
        self._stop_request.clear()

    