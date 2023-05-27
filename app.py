
import os
from datetime import datetime

from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, login_required, current_user, logout_user

from automod.automod import Goku
from instancedb.instancedb import Piccolo
from app_utils import ComponentManager, Logging, SettingsManager

from mastodon import Mastodon

# Some fixed config values
CONFIG_FILE = "global_config.json"
APP_BASE_URL = "http://halcy.de:5000/"
SECRET_KEY = "lol secret" # Doesn't matter much since we invalidate all sessions every restart
CONNECTED_INSTANCE = "https://icosahedron.website"
CLIENT_CRED_FILE = "mastomod_client_cred_admin_danger.secret"

# Initialize the application component manager
component_manager = ComponentManager()
component_manager.register_component("logging", Logging())
component_manager.register_component("settings", SettingsManager(CONFIG_FILE, component_manager))
component_manager.register_component("instance_db", Piccolo(component_manager))
component_manager.register_component("goku", Goku(component_manager), True)

# Set up flask
app = Flask(__name__)
app.secret_key = SECRET_KEY + f"{os.urandom(32)}"

# Set up a mastodon app
# User credentials are not stored, they are saved in memory only on first login
if not os.path.exists(CLIENT_CRED_FILE):
    # Register app if no app credentials found
    with app.app_context():
        Mastodon.create_app(
            "ModTools",
            api_base_url = CONNECTED_INSTANCE,
            scopes = ["read", "write", "follow", "push", "admin:read", "admin:write"],
            to_file = CLIENT_CRED_FILE,
            redirect_uris = APP_BASE_URL + "authorize"
        )

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)

"""
Jinja tooling
"""
@app.template_filter('strftime')
def _jinja2_filter_datetime(timestamp, fmt=None):
    date = datetime.fromtimestamp(timestamp)
    fmt = fmt or '%Y-%m-%d %H:%M:%S'
    return date.strftime(fmt)

@app.template_filter('is_boolean')
def _jinja2_filter_is_boolean(value):
    return isinstance(value, bool)

@app.template_filter('is_number')
def _jinja2_filter_is_number(value):
    return isinstance(value, (int, float))

@app.template_filter('is_list')
def _jinja2_filter_is_list(value):
    return isinstance(value, list)

"""
Auth routes and data
"""
class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

@app.route("/login")
def login():
    """
    Login route that redirects to masto instance oauth url
    """
    # Redirect user to Mastodon login page
    mastodon = Mastodon(client_id = CLIENT_CRED_FILE)
    return redirect(mastodon.auth_request_url(
        scopes = ["read", "write", "follow", "push", "admin:read", "admin:write"],
        redirect_uris = APP_BASE_URL + "authorize"
    ))

@app.route('/authorize')
def authorize():
    """
    Oauth target
    """
    code = request.args.get('code')
    mastodon = Mastodon(client_id = CLIENT_CRED_FILE)
    mastodon.log_in(
        code = code,
        redirect_uri = APP_BASE_URL + "authorize",
        scopes = ["read", "write", "follow", "push", "admin:read", "admin:write"],
    )
    account = mastodon.account_verify_credentials()
    if account.role.name != "Owner": # TODO: Add perm decoding to mastopy, then fix this to go off perms instead of role name
        return 'Access denied', 403

    # We have a valid admin account - store it as a component if none exists
    if not component_manager.have_component("mastodon"):
        component_manager.register_component("mastodon", mastodon)

    # Log in user
    user = User(account['id'])
    login_user(user)
    return redirect(url_for('home'))

@app.route('/logout')
@login_required
def logout():
    """
    Logout route. Doesn't actually invalidate the mastodon side session (for now), just locally logs user out
    """
    logout_user()
    return redirect(url_for('home'))

"""
Main UI
"""
@app.route("/start/<component>", methods=["POST"])
@login_required
def start_component(component):
    """
    Component starter
    """
    if not component_manager.is_bg_processing_component(component):
        return jsonify({"error": f"No such component: {component}"}), 404
    component_manager.get_component(component).start()
    return render_component(component)

@app.route("/stop/<component>", methods=["POST"])
@login_required
def stop_component(component):
    """
    Component stopper
    """
    if not component_manager.is_bg_processing_component(component):
        return jsonify({"error": f"No such component: {component}"}), 404
    component_manager.get_component(component).stop()
    return render_component(component)

@app.route("/state/<component>", methods=["GET"])
@login_required
def get_state(component):
    """
    Returns state so we can display appropriate buttons
    """
    if not component_manager.is_bg_processing_component(component):
        return jsonify({"error": f"No such component: {component}"}), 404
    return render_component(component)

def render_component(component):
    return render_template('component.html', component_name=component, component=component_manager.get_component(component))

@app.route('/logs', methods=['GET'])
@login_required
def get_logs():
    """
    Returns logs
    """
    logging_component = component_manager.get_component("logging")
    if logging_component:
        return render_template('logs.html', logs=logging_component.get_log())
    else:
        return jsonify({"error": "No logging component found"}), 404

@app.route('/settings', methods=['GET'])
@login_required
def get_settings():
    settings_manager = component_manager.get_component("settings")
    if settings_manager:
        return render_template('settings.html', settings=settings_manager.get_config())
    else:
        return jsonify({"error": "No settings manager component found"}), 404

@app.route('/settings', methods=['POST'])
@login_required
def update_settings():
    settings_manager = component_manager.get_component("settings")
    if settings_manager:
        for setting, value in request.json.items():
            component, key = setting.split("/")
            if isinstance(settings_manager.get_config(component)[key], list):
                value = [x.strip() for x in value.split(',')]
            elif isinstance(settings_manager.get_config(component)[key], bool):
                value_maybe = False
                if value.lower() == "true":
                    value_maybe = True
                value = value_maybe
            elif isinstance(settings_manager.get_config(component)[key], int):
                value = int(value)
            elif isinstance(settings_manager.get_config(component)[key], float):
                value = float(value)
            else:
                value = str(value)
            settings_manager.set_config_value(component, key, value)
        return render_template('settings.html', settings=settings_manager.get_config())
    else:
        return jsonify({"error": "No settings manager component found"}), 404

@app.route('/')
@login_required
def home():
    return render_template('index.html', components=component_manager.get_components_with_bg_processing())

if __name__ == "__main__":
    app.run(host = "0.0.0.0", port = 5000)
