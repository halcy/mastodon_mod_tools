import traceback
import pickle as pkl 
import threading
import time
import os
class Bulma:
    """
    It's Bulma - Bot Utility for Logging Moderation Activity
    """
    def __init__(self, component_manager):
        """
        Component init.
        """
        self.component_manager = component_manager

        # Storage for reports, and reported user IDs
        self.report_db = {}
        self.reported_users = {}
        self.time_since_last_report_update = 0
        self.report_update_interval = 120
        self.report_update_lock = threading.Lock()

        # Thread stuff
        self._is_running = threading.Event()
        self._stop_request = threading.Event()
        self._worker_thread = None

        # Load report database
        store_path = self.component_manager.get_component("settings").get_config("bulma")["report_db_file"]
        if os.path.exists(store_path):
            self.report_db = pkl.load(open(store_path, "rb"))

    def start(self):
        """
        Start thread, if not running
        """
        if not self._is_running.is_set():
            self.component_manager.get_component("logging").add_log("Bulma", "Info", "Starting component")
            self._stop_request.clear()
            self._is_running.set()
            self._worker_thread = threading.Thread(target=self.update_report_loop, daemon=True)
            self._worker_thread.start()
            
    def stop(self):
        self._stop_request.set()
        self.component_manager.get_component("logging").add_log("Bulma", "Info", "Stop requested")
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

    def update_report_loop(self):
        while not self._stop_request.is_set():
            try:
                self.update_reports()
                time.sleep(self.report_update_interval)
            except Exception:
                exc_str = traceback.format_exc()
                self.component_manager.get_component("logging").add_log("Bulma", "Error", f"An error occurred in the report update loop: {exc_str}")
                time.sleep(self.report_update_interval)
        self.component_manager.get_component("logging").add_log("Bulma", "Info", "Component stopped")
        self._is_running.clear()
        self._stop_request.clear()

    def update_reports(self):
        """
        Update the report database
        """
        try:
            if time.time() - self.time_since_last_report_update > self.report_update_interval:
                if self.report_update_lock.acquire(blocking=False):
                    self.component_manager.get_component("logging").add_log("Bulma", "Info", "Processing report update")
                    self.time_since_last_report_update = time.time()
                    for report in self.component_manager.get_component("mastodon").admin_reports(resolved=False):
                        self._process_report_dict_internal(report)
                    for report in self.component_manager.get_component("mastodon").admin_reports(resolved=True):
                        self._process_report_dict_internal(report)

                    # See if we can autoclose reports
                    check_reports = list(self.reported_users.values())
                    for report in check_reports:
                        self.component_manager.get_component("logging").add_log("Bulma", "Trace", f"Checking user {report['target_account']['id']} for autoclose")
                        try:
                            account = self.component_manager.get_component("mastodon").account(report['target_account']['id'])
                        except:
                            self.component_manager.get_component("mastodon").admin_report_resolve(report)
                            self.reported_users.pop(report['target_account']['id'], None)
                            self.component_manager.get_component("logging").add_log("Bulma", "Info", f"Closed report {report['id']} due to user not found")
                        
                        # Get statuses
                        nb_bad_statuses = self.component_manager.get_component("settings").get_config("bulma")["autoclose_bad_status_nb"] 
                        bad_status_thresh = self.component_manager.get_component("settings").get_config("bulma")["autoclose_bad_status_thresh"] 
                        goku = self.component_manager.get_component("goku")
                        statuses = self.component_manager.get_component("mastodon").account_statuses(account, limit = nb_bad_statuses)
                        if len(statuses) < nb_bad_statuses:
                            self.component_manager.get_component("logging").add_log("Bulma", "Trace", f"User {report['target_account']['id']} has too few statuses")
                            continue

                        # We have enough statuses, check them all
                        bad_statuses = 0
                        for status in statuses:
                            # Evaluate status via goku
                            user_dict = status["account"]
                            eval_reports = goku.eval_user(user_dict, [status], update_history = False, check_types = ["status"], check_history = False)
                            if len(eval_reports) != 0:
                                if eval_reports[0][2] >= bad_status_thresh:
                                    bad_statuses += 1

                        self.component_manager.get_component("logging").add_log("Bulma", "Trace", f"User {report['target_account']['id']} has {bad_statuses} bad statuses")
                        if bad_statuses == nb_bad_statuses:
                            # Too many bad statuses, close report and suspend
                            self.component_manager.get_component("mastodon").admin_account_moderate(report['target_account']['id'], action="suspend", report_id = report)
                            self.reported_users.pop(report['target_account']['id'], None)
                            self.component_manager.get_component("logging").add_log("Bulma", "Info", f"Closed report {report['id']} and suspended user {report['target_account']['id']}")

                    # Store database
                    store_path = self.component_manager.get_component("settings").get_config("bulma")["report_db_file"]
                    pkl.dump(self.report_db, open(store_path, "wb"))
        except:
            # Log failure
            exc_str = traceback.format_exc()
            self.component_manager.get_component("logging").add_log("Bulma", "Error", f"Failed to update reports: {exc_str}")
        finally:
            # Release lock if we have it
            if self.report_update_lock.locked():
                self.report_update_lock.release()

    def process_report_dict(self, report_dict):
        """
        Process a report dict and add it to the report database
        """
        self._process_report_dict_internal(report_dict)
        self.update_reports()

    def _process_report_dict_internal(self, report_dict):
        """
        Process a report dict and add it to the report database
        """
        self.component_manager.get_component("logging").add_log("Bulma", "Trace", f"Processing report {report_dict['id']}")
        self.report_db[report_dict['id']] = report_dict
        
        # If report open: add user to reported users. If report closed: remove user from reported users
        if report_dict["action_taken"] == False:
            self.component_manager.get_component("logging").add_log("Bulma", "Trace", f"Adding user {report_dict['target_account']['id']} to reported users")
            self.reported_users[report_dict['target_account']['id']] = report_dict
        else:
            # Remove but no error if not there
            self.component_manager.get_component("logging").add_log("Bulma", "Trace", f"Removing user {report_dict['target_account']['id']} from reported users")
            self.reported_users.pop(report_dict['target_account']['id'], None)