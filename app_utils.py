import time
import json
from shutil import move

class LogEntry:
    """
    A log message
    """
    def __init__(self, timestamp, component, severity, message):
        self.timestamp = timestamp
        self.component = component
        self.severity = severity
        self.message = message

class Logging:
    """
    Basig log message storage
    """
    def __init__(self, max_logs=2000, severities = ["Debug", "Info", "Warn", "Error", "Fatal"]):
        self.logs = []
        self.max_logs = max_logs
        self.severities = severities

    def add_log(self, component, severity, message):
        timestamp = time.time()
        log_entry = LogEntry(timestamp, component, severity, message)
        if not severity in self.severities:
            return
        
        self.logs.append(log_entry)

        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs:]

    def get_log(self, n=None):
        if n is None:
            return self.logs
        else:
            return self.logs[-n:]

class SettingsManager:
    """
    Another mostly-a-dict-wrapper but it loads and stores from/to a json file
    """
    def __init__(self, config_path, component_manager):
        self.path = config_path
        self.temp_path = config_path + ".tmp"
        self.config = json.load(open(self.path, 'rb'))
        self.component_manager = component_manager
    
    def get_config(self, component = None):
        if component is None:
            # Return config but without "base" component
            return {x: self.config[x] for x in self.config if x != "base"}
        else:
            return self.config[component]

    def set_config_value(self, component, key, value):
        # There's a potential data race here if two people try to edit the config at the same time, but that largely just woN't matter
        dirty = False
        if self.config[component][key] != value:
            self.component_manager.get_component("logging").add_log("settings", "warn", f"changing setting {key} from {self.config[component][key]} to {value}.")
            self.config[component][key] = value
            dirty = True
        if dirty:
            # Atomic-write config
            json.dump(self.config, open(self.temp_path, 'w'))
            move(self.temp_path, self.path)

class ComponentManager:
    """
    Mostly a dict wrapper
    """
    def __init__(self):
        self.components = dict()
        self.components_with_bg_processing = set()

    def register_component(self, component_name, component, managed_bg_processing = False):
        self.components[component_name] = component
        if managed_bg_processing:
            self.components_with_bg_processing.add(component_name)

    def get_component(self, component_name):
        return self.components[component_name]

    def have_component(self, compoent_name):
        return compoent_name in self.components

    def is_bg_processing_component(self, compoent_name):
        return compoent_name in self.components_with_bg_processing

    def get_components_with_bg_processing(self):
        return{x: self.components[x] for x in list(self.components_with_bg_processing)}