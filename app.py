
import os
from datetime import datetime
import traceback
import hashlib
import hmac

from flask import Flask, request, jsonify, render_template, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user

from automod.automod import Goku
from instancedb.instancedb import Piccolo
from reportdb.reportdb import Bulma
from app_utils import ComponentManager, Logging, SettingsManager

from mastodon import Mastodon

# where do we load the config from?
CONFIG_FILE = "global_config.json"

# Initialize the application component manager
component_manager = ComponentManager()
component_manager.register_component("settings", SettingsManager(CONFIG_FILE, component_manager))
component_manager.register_component("logging", Logging(component_manager))
component_manager.register_component("piccolo", Piccolo(component_manager))
component_manager.register_component("goku", Goku(component_manager), True)
component_manager.register_component("bulma", Bulma(component_manager), True)

# Load base config data
if component_manager.get_component("settings").get_config("base")["i_promise_to_be_really_careful"] == False:
    assert False, "You must set i_promise_to_be_really_careful to true in the config file to run this app"
    
APP_BASE_URL = component_manager.get_component("settings").get_config("base")["app_base_url"]
SECRET_KEY = component_manager.get_component("settings").get_config("base")["app_session_secret"] # we append a random string to this later (e.g. sessions invalidate every restart)
CONNECTED_INSTANCE = component_manager.get_component("settings").get_config("base")["connected_instance"]
CLIENT_CRED_FILE = component_manager.get_component("settings").get_config("base")["client_cred_file"] 

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
Webhook tooling
"""
def check_signature(request, conf_key):
    """
    Check webhook signature
    """
    try:
        signature_header = request.headers.get('X-Hub-Signature')
        if signature_header is None:
            return False
        _, signature = signature_header.split('=')
        settings_manager = component_manager.get_component("settings")
        webhook_secret = settings_manager.get_config("base")[conf_key].encode("utf8")
        hashed = hmac.new(webhook_secret, request.get_data(), hashlib.sha256)
        digest = hashed.hexdigest()
        if not hmac.compare_digest(digest, signature):
            return False
    except:
        return False
    return True
                

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
    """
    Returns settings, as an edit form
    """
    settings_manager = component_manager.get_component("settings")
    if settings_manager:
        return render_template('settings.html', settings=settings_manager.get_config())
    else:
        return jsonify({"error": "No settings manager component found"}), 404

@app.route('/settings', methods=['POST'])
@login_required
def update_settings():
    """
    Updates settings
    """
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

@app.route('/instance_info', methods=['GET', 'POST'])
@login_required
def instance_info():
    """
    Returns info for a given instance
    """
    piccolo = component_manager.get_component("piccolo")
    if request.method == 'POST':
        instance_name = request.form.get('instance_name')
        instance_url, last_updated, instance_info = piccolo.get_nodeinfo(instance_name)
        if instance_info is not None:
            last_updated = datetime.fromtimestamp(last_updated).strftime('%Y-%m-%d %H:%M:%S')

            # Ensure all required attributes are present in instance_info
            if 'software' not in instance_info:
                instance_info['software'] = {}
            if 'usage' not in instance_info:
                instance_info['usage'] = {}
            if 'users' not in instance_info['usage']:
                instance_info['usage']['users'] = {}
            
            return render_template('instance_search.html', instance_url=instance_url, last_updated=last_updated, instance_info=instance_info)
        else:
            flash('No information found for this instance', 'error')
            return render_template('instance_search.html')
    else: 
        return render_template('instance_search.html')

@app.route('/webhook_status', methods=['GET', 'POST'])
def webhook_status():
    """
    Run goku for one status (webhook target)
    """
    try:
        # Get components
        settings_manager = component_manager.get_component("settings")
        goku = component_manager.get_component("goku")
        piccolo = component_manager.get_component("piccolo")
        if settings_manager is not None and goku is not None and not piccolo is None:
            # Verify signature
            if not check_signature(request, "webhook_secret_status"):
                return jsonify({"error": "Invalid secret"}), 403

            # Parse request
            post_dict = request.json
            status_object = post_dict["object"]
            account_object = status_object["account"]
            
            # At this time, if other instance reports that they are closed-reg, trust that information and stop here
            if component_manager.get_component("piccolo").is_closed_regs_instance(account_object["acct"].split("@")[-1]):
                return jsonify({"status": "ok"})
            
            # Otherwise, run goku
            reports = goku.eval_user(account_object, [status_object], update_history = False, check_types=["status"])

            # File reports
            if len(reports) > 0:
                goku.generate_reports(reports, allow_suspend = False)
                component_manager.get_component("logging").add_log("Base", "Info", f"Generated report in webhook for {account_object['acct']}")
                return jsonify({"status": "bad"}) # ideally we would return something here that makes mastodon hold notifications
            return jsonify({"status": "ok"})
        else:
            return jsonify({"error": "Not ready"}), 404
    except Exception:
        exc_str = traceback.format_exc()
        component_manager.get_component("logging").add_log("Base", "Error", f"Error in status check webhook: {exc_str}")
    return jsonify({"error": "Internal error"}), 500

@app.route('/webhook_report', methods=['GET', 'POST'])
def webhook_report():
    """
    Run bulma for a new report (webhook target)
    """
    try:
        # Get components
        bulma = component_manager.get_component("bulma")
        if bulma is not None:
            # Verify signature
            if not check_signature(request, "webhook_secret_report"):
                return jsonify({"error": "Invalid secret"}), 403

            # Parse request
            report = request.json["object"]
            bulma.process_report_dict(report)
            return jsonify({"status": "ok"})
        else:
            return jsonify({"error": "Not ready"}), 404
    except Exception:
        exc_str = traceback.format_exc()
        component_manager.get_component("logging").add_log("Base", "Error", f"Error in report check webhook: {exc_str}")
    return jsonify({"error": "Internal error"}), 500

@app.route('/autocomplete_instance')
@login_required
def autocomplete_instance():
    """
    Instance autocompleter
    """
    piccolo = component_manager.get_component("piccolo")
    name = request.args.get('name', '')
    instances = piccolo.search_instance(name)
    return jsonify(instances)

@app.route('/')
@login_required
def home():
    """
    Root route, as it were
    """
    return render_template('index.html', components=component_manager.get_components_with_bg_processing())

if __name__ == "__main__":
    app.run(host = "0.0.0.0", port = 5000)
