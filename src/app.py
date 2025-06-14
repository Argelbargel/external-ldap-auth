from os import getenv
from random import choices
from string import ascii_letters, digits
from urllib.parse import urlparse

from flask import Flask
from flask import abort, request, render_template
from werkzeug.exceptions import HTTPException
from cachetools import TTLCache, cached
from prometheus_flask_exporter import PrometheusMetrics
from prometheus_client import Counter

from lib.authentication import HtPasswd, LDAP
from lib.authorization import Rule, RulesFile, parse_rules
from lib.bruteforce import BruteForce
from lib.logging import Logger, configure_logging


# --- Logging -----------------------------------------------------------------
configure_logging(getenv('LOG_LEVEL', 'INFO'), getenv('LOG_FORMAT', 'JSON'))
log = Logger('main')


# --- Authentication & Authorization ------------------------------------------
authBackends = []
htpasswdFile = getenv('HTPASSWD_FILE_PATH', '.config/.htpasswd')
if htpasswdFile:
    log.info("using htpasswd authentication backend", htpasswdFile=htpasswdFile, groupFile=getenv('HTPASSWD_GROUP_FILE_PATH'))
    authBackends.append(
        HtPasswd(
            getenv('HTPASSWD_FILE_PATH', '.config/.htpasswd'),
            getenv('HTPASSWD_GROUP_FILE_PATH')
        )
    )

ldapEndpoint = getenv('LDAP_SERVER_URL', '')
if ldapEndpoint:
    log.info("using ldap authentication backend", ldapServer=ldapEndpoint, bindDn=getenv('LDAP_BIND_DN'), searchBase=getenv('LDAP_SEARCH_BASE'), searchFilter=getenv('LDAP_SEARCH_FILTER'))
    authBackends.append(
        LDAP(
            ldapEndpoint, getenv('LDAP_BIND_DN'),
            getenv('LDAP_SEARCH_BASE'), getenv('LDAP_SEARCH_FILTER'),
            getenv('LDAP_MANAGER_DN'), getenv('LDAP_MANAGER_PASSWORD')
        )
    )

if not authBackends:
    log.warning("No authentication-backends configured - the service will reject any non-public requests!")

default_rules = RulesFile(getenv("AUTHORIZATION_RULES_PATH", "./config/rules.conf"))
authCache = TTLCache(float('inf'), float(getenv("AUTH_CACHE_TTL_SECONDS", "15")))

INGRESS_RULES_ENABLED = getenv('AUTHORIZATION_INGRESS_RULES_ENABLED', 'false').lower() == 'true'
INGRESS_RULES_SECRET  = getenv('AUTHORIZATION_INGRESS_RULES_SECRET', '') or ''.join(choices(ascii_letters + digits, k=32))
if INGRESS_RULES_ENABLED:
    log.warning("authorization-rules from ingresses are enabled")


# --- Brute-Force-Protection --------------------------------------------------
bruteForce = BruteForce(
                getenv('BRUTE_FORCE_PROTECTION_ENABLED', "false").lower() == "true",
                int(getenv('BRUTE_FORCE_MAX_FAILURE_COUNT', "5")),
                int(getenv('BRUTE_FORCE_EXPIRATION_SECONDS', "60"))
            )

# --- Flask -------------------------------------------------------------------
app = Flask(__name__)
app.config.from_object(__name__)

# --- Metrics -------------------------------------------------------------------
metrics = PrometheusMetrics(app, group_by='endpoint', excluded_paths='^/(?!$)', default_latency_as_histogram=False)
blocked_ips = Counter('blocked_ips', 'IPs blocked by brute-force-protection', ['ip'])


def _request_host():
    return _parse_request_url().hostname

def _request_path():
    return _parse_request_url().path


# --- Routes ------------------------------------------------------------------
@app.route('/', methods=['GET'])
@metrics.counter('auth_requests', 'Authentication requests by hosts and status',
                 labels={'host': _request_host, 'status': lambda r: r.status_code})
def auth():
    ip = _request_ip()
    host = _request_host()
    rule = _find_rule(host, ip, request.method, _request_path())
    headers = []

    if rule.is_forbidden():
        return abort(403)

    if not rule.is_public():

        if not request.authorization:
            log.debug('Missing authorization-header')
            return abort(401)

        username = request.authorization.username
        password = request.authorization.password

        if bruteForce.is_blocked(ip):
            log.info('Rejecting request from blocked ip', ip=ip, host=host, username=username)
            return abort(429)

        authenticated, usergroups = _authenticate_(username, password)
        if not authenticated:
            log.info('Authentication failed', ip=ip, username=username)

            if bruteForce.add_failure(ip):
                log.warning('Blocking requests after to many authentication failures', ip=ip, host=host, username=username)
                blocked_ips.labels(ip=ip).inc()
                return abort(429)

            return abort(401)

        log.debug('Authentication successful', ip=ip, host=host, username=username, groups=usergroups)

        authorized, matched_groups = rule.authorize(username, usergroups)
        if not authorized:
            log.info('Authorization failed', ip=ip, host=host, method=request.method, path=_request_path(), username=username)
            return abort(403)

        headers=[('x-user', username),('x-groups', ",".join(matched_groups))]
        log.debug('Authorization successful', ip=ip, host=host, method=request.method, path=_request_path(), username=username, groups=matched_groups)

    return _render_response(200, "Authorized", "You are authorized to access the requested resource", headers=headers)


@app.route('/health', methods=['GET'])
def health():
    for b in authBackends:
        if not b.health():
            return abort(503)

    return _render_response(200, "Healthy", "External Ingress Authentication is healthy")


@app.after_request
def global_headers(response):
    response.headers['Server'] = '' # Remove Server header
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


@app.errorhandler(HTTPException)
def handle_exception(e):
    if e.code not in [401, 403, 404, 405, 429]:
        log.error('An error occurred while processing a request', ip=_request_ip(), path=request.path, code=e.code, name=e.name, description=e.description)

    return _render_response(e.code, e.name, e.description, "An Error Occurred While Processing Your Request")


@cached(authCache)
def _authenticate_(username, password):
    for b in authBackends:
        authenticated, groups = b.authenticate(username, password)
        if authenticated:
            log.trace("Authenticated successful", backend=b.__class__.__name__, username=username)
            return True, groups
    return False, []


@cached(authCache)
def _find_rule(host:str, ip:str, method:str, path:str) -> Rule:
    rules = default_rules

    if INGRESS_RULES_ENABLED and 'X-Authorization-Rules' in request.headers:
        if 'X-External-Auth-Secret' not in request.headers or request.headers.get('X-External-Auth-Secret') != INGRESS_RULES_SECRET:
            log.warning("ignoring authorization rules from ingress as secret is missing or invalid", host=host)
        else:
            ingress_rules = parse_rules(request.headers.get('X-Authorization-Rules'))
            if ingress_rules:
                log.info("using authorization-rules provided by ingress...", host=host)
                log.debug("authorization-rules provided by ingress", host=host, rules=ingress_rules)
                rules = ingress_rules

    rule = rules.find_rule(host, ip, method, path)
    log.info(f"Authenticating request using rule {rule}...", host=host, ip=ip, method=method, path=path)
    return rule


def _request_ip():
    # Nginx Ingress Controller returns the X-Forwarded-For in X-Original-Forwarded-For
    # The last IP from the list is the client IP
    if request.environ.get('HTTP_X_ORIGINAL_FORWARDED_FOR') is not None:
        nginxControllerIP = request.environ.get('HTTP_X_ORIGINAL_FORWARDED_FOR')
        nginxControllerIP = [x.strip() for x in nginxControllerIP.split(',')]
        return nginxControllerIP[-1]

    if request.environ.get('HTTP_X_REAL_IP') is not None:
        return request.environ.get('HTTP_X_REAL_IP')

    if request.environ.get('HTTP_X_FORWARDED_FOR') is not None:
        return request.environ.get('HTTP_X_FORWARDED_FOR')

    return request.remote_addr


def _parse_request_url():
    url = request.url
    if request.environ.get('HTTP_X_ORIGINAL_URL') is not None:
        url = request.environ.get('HTTP_X_ORIGINAL_URL')
    return urlparse(url)


def _render_response(status_code, title, message, error = None, headers = None):
    if headers is None:
        headers = []

    layout = {
        'error': error,
        'title': title,
        'message': message
    }

    if status_code == 401:
        headers.append(('WWW-Authenticate', 'Basic realm=External Authentication'))

    return render_template('page.html', layout=layout), status_code, headers


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9000, debug=True, use_reloader=True)
