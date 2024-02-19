import time
import threading

class Vegeta():
    """
    It's Vegeta - Verification and Evaluation for Graylisting Entities with Targeted Assessment

    WIP, many parts in progress or to be further thought about
    also missing logging
    """
    def __init__(self, component_manager):
        self.component_manager = component_manager
        self.list_cache = {}
        self.lists_pull_lock = threading.Lock()
        self.lists_pull_interval = 300
        self.last_lists_pull = 0
        self.had_successful_pull = False
        self.update_lists()

    def update_lists(self):
        """
        Pull the list of domain blocks and update the list cache if necessary

        If starting for the first time: Also pull the list of *all* peers and mark any that have
        not been limited/suspended as okay.
        """
        if time.time() - self.last_lists_pull > self.lists_pull_interval:
            try:
                if self.lists_pull_lock.acquire(blocking=False):
                    # Start building the new list cache
                    new_list_cache = {}
                    
                    # On first pull: Get list of peers and mark them okay (TODO: trustlist? where to store it?)
                    if not self.had_successful_pull:
                        peers = mastodon.instance_peers()
                        for peer in peers:
                            new_list_cache[peer] = {
                                "domain": peer,
                                "status": "ok",
                            }

                    # Get the list of domain blocks and update the list
                    self.last_lists_pull = time.time()
                    domain_block_list = self.component_manager.get_component("mastodon").admin_domain_blocks()
                    for block in domain_block_list:
                        block_data = {
                            "domain": block["domain"],
                            "created_at": block["created_at"],
                            "private_comment": block["private_comment"],
                            "public_comment": block["public_comment"],
                        }
                        if block["severity"] == "silence":
                            if "automod:graylisted" in block["private_comment"]:
                                block_data["status"] = "graylisted"
                            else:
                                block_data["status"] = "silenced"
                        elif block["severity"] == "suspend":
                            block_data["status"] = "suspended"
                        else:
                            block_data["status"] = "other"
                        new_list_cache[block["domain"]] = block_data
                    self.list_cache = new_list_cache
                    self.had_successful_pull = True
            finally:
                if self.lists_pull_lock.locked():
                    self.lists_pull_lock.release()

    def try_apply_graylisting(self, domain):
        """
        Apply graylisting to an instance if we are seeing it for the first time ever
        Should likely be called from the user creation webhook to get in as early as possible.

        Return a tuple: (is_graylisted, newly_graylisted)

        TODO: hard-graylist, a marker for instances that we actually have seen spam from.
        """
        # No list? Stop
        self.update_lists()
        if not self.had_successful_pull:
            return False, False
        
        # In cache? Return True if graylisted, otherwise False
        if domain in self.list_cache:
            if self.list_cache[domain]["status"] =="graylisted":
                return True, False
            else:
                return False, False
        
        # Not in cache? Graylist it
        self.component_manager.get_component("mastodon").admin_create_domain_block(domain, "silence", private_comment = "automod:graylisted", public_comment = "Graylisted (auto)")
        self.list_cache[domain] = {
            "domain": domain,
            "status": "graylisted",
        }
        return True, True
        

    def check_user(self, account, statuses = None):
        """
        Call from status webhook to check if we can unsilence a user

        This has to be Real Fast since it is called for every status
        """   
        # 0. If the instance is not present in the info list: graylist it
        is_graylisted, newly_graylisted = self.try_apply_graylisting(account["acct"].split("@")[1])
        if not is_graylisted:
            return
        
        # 1. If user is not limited: stop, nothing else do to
        is_limited = newly_graylisted
        if "limited" in account and account["limited"]:
            is_limited = True
        if not is_limited:
            return
        
        # 3. Check pending reports. If any: stop, nothing to do
        bulma = self.component_manager.get_component("bulma")
        if account["id"] in bulma.reported_users:
            return
        
        # 4. Start checking conditions in order of effort. Currently there is only one.
        # 4.1. "number of okay statuses" > threshold: remove limit
        ok_status_nb = self.component_manager.get_component("settings").get_config("vegeta")["ok_status_nb"] 
        if statuses is None or len(statuses) < ok_status_nb:
            statuses = self.component_manager.get_component("mastodon").account_statuses(account["id"], limit=ok_status_nb)
        if len(statuses) < ok_status_nb:
            return
        
        ok_status_threshold = self.component_manager.get_component("settings").get_config("bulma")["ok_status_threshold"]
        ok_status_count = 0
        goku = self.component_manager.get_component("goku")
        for status in statuses: # TODO do this with webhook and internal housekeeping instead?
            eval_reports = goku.eval_user(account, [status], update_history = False, check_types = ["status"], check_history = False) # TODO goku should probably have a memoize cache that it uses where possible
            if len(eval_reports) == 0 or eval_reports[0][2] < ok_status_threshold:
                ok_status_count += 1
        if ok_status_count < ok_status_nb:
            return
        
        # 5. If we got here: Remove limit
        self.component_manager.get_component("mastodon").admin_account_unsilence(account["id"])

        # 6. Maybe unlimit instance? how do we housekeep that? What are the conditions?
        # Maybe: "closed reg detected" or "open reg detected but not hard-graylisted",
        # plus "have seen X okay users" (where X is a setting) [counting will be annoying
        # if we want to not hit server too much]
        # TODO
