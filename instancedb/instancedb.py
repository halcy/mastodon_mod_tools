# Basic caching instance db

import time
import threading
import os
import pickle as pkl
from mastodon import Mastodon

class Piccolo:
    """
    It's Piccolo, the Platform for Instance Cataloging (with Cache Of Last Operations)
    """
    def __init__(self, component_manager, max_cache_age_seconds = 43200):
        self.max_cache_age_seconds = max_cache_age_seconds
        self.instance_cache = {}
        self.component_manager = component_manager
        self.last_store = time.time()
        self.store_lock = threading.Lock()
        self.store_interval = 60

        cache_file = component_manager.get_component("settings").get_config("piccolo")["cache_file"]
        if os.path.exists(cache_file):
            self.instance_cache = pkl.load(open(cache_file, 'rb'))

    def normalize_instance_url(self, instance_url):
        """
        Trim protocols

        This will leave any non-standard protocols on
        """
        if instance_url.startswith("http://"):
            instance_url = instance_url[7:]
        elif instance_url.startswith("https://"):
            instance_url = instance_url[8:]
        return instance_url

    def update_nodeinfo(self, instance_url):
        """
        Try to find nodeinfo and update cache
        """
        instance_url = self.normalize_instance_url(instance_url)
        self.component_manager.get_component("logging").add_log("Piccolo", "Info", f"Fetching nodeinfo for {instance_url}")
        instance_info = None
        try:
            instance_info = Mastodon(api_base_url = f"https://{instance_url}", version_check_mode="none").instance_nodeinfo()
        except:
            pass
        if instance_info is None:
            try:
                instance_info = Mastodon(api_base_url = f"http://{instance_url}", version_check_mode="none").instance_nodeinfo()
            except:
                pass
        if not instance_info is None:
            self.instance_cache[instance_url] = (time.time(), instance_info)

            # Possibly write to file
            if self.store_lock.acquire(blocking=False):
                try:
                    if time.time() - self.last_store > self.store_interval:
                        cache_file = self.component_manager.get_component("settings").get_config("piccolo")["cache_file"]
                        with open(cache_file, 'wb') as f:
                            pkl.dump(self.instance_cache, f)
                        self.last_store = time.time()
                        self.component_manager.get_component("logging").add_log("Piccolo", "error", f"Stored instance db cache")
                except Exception as e:
                    self.component_manager.get_component("logging").add_log("Piccolo", "error", f"Failed to store instance db cache: {e}")
                finally:
                    self.store_lock.release()

        if instance_url in self.instance_cache:
            return self.instance_cache[instance_url]
        else:
            self.component_manager.get_component("logging").add_log("Piccolo", "error", f"Retrieving info failed for {instance_url}")
            return (-1, None)

    def search_instance(self, name):
        """
        Find instances from the cache
        """
        return [k for k in self.instance_cache if name in k]

    def get_nodeinfo(self, instance_url):
        """
        Get nodeinfo, update if needed
        """
        instance_url = self.normalize_instance_url(instance_url)
        instance_last_update = -1
        instance_info = None
        if instance_url in self.instance_cache:
            instance_last_update, instance_info = self.instance_cache[instance_url]
        if time.time() - instance_last_update > self.max_cache_age_seconds:
            instance_last_update, instance_info = self.update_nodeinfo(instance_url)
        return (instance_url, instance_last_update, instance_info)
    
    def is_closed_regs_instance(self, instance_url):
        """
        Determine if an instance for-sure reports that registrations are closed
        """
        is_closed = False
        try:
            is_closed = self.get_nodeinfo(instance_url)[2]["openRegistrations"] == False
        except:
            pass
        return is_closed
