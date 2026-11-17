#!/usr/bin/env python3
"""Held-out synthetic corpus generator for a secret/credential scanner.

Author : an independent agent (Claude) that never saw the scanner under test,
         its rules, or any earlier corpus. Written without reading any other
         file, repository or tool output. No network, standard library only.
Date   : 2026-11-17
Values : EVERY credential value is synthetic: random characters generated at
         runtime in the publicly documented FORMAT of each provider (prefix,
         length, alphabet). No real credential and no provider documentation
         example appears in the positive set; documentation examples appear
         only in the negative set (assembled from fragments at runtime).
         For URI, webhook, PEM, OpenSSH, JWT and Basic-auth families the
         `secret` field is the whole credential-bearing string (for Basic
         auth: the base64 blob as typed after "Basic "). In single-line
         wrappers a multi-line secret is flattened with literal \\n escapes.

Positive families (24, 50 items each):
  aws_access_key_id, aws_secret_access_key, github_token,
  github_fine_grained_pat, gitlab_pat, slack_token, slack_webhook_url,
  discord_bot_token, telegram_bot_token, stripe_live_key, openai_api_key,
  anthropic_api_key, other_llm_key, google_api_key, sendgrid_api_key, jwt,
  pem_private_key, openssh_private_key, db_connection_uri,
  password_assignment, http_authorization, session_cookie,
  package_registry_token, ci_token

Encodings (38). Value transforms:
  plain, base64, base64_nopad, base64url, base64url_nopad, hex_lower,
  hex_upper, url_encoded, url_double_encoded, json_escaped,
  json_unicode_escaped, base64_wrap64, base64_wrap76, quoted_printable,
  html_entity_named, html_entity_numeric, reversed, split_concat_literal,
  zero_width_inserted, double_base64, base64_of_assignment,
  json_nested_escaped
Syntax wrappers (plain value inside the construct):
  shell_command, curl_line, dotenv, yaml, toml, json_document, ini,
  python_literal, js_literal, dockerfile_env, log_line, stack_trace,
  markdown_fence, bracketed_punctuation, backslash_continuation,
  mixed_case_keyword
If a transform would leave a value unchanged (e.g. url-encoding a hex string)
the generator falls through to the next encoding, and the label reflects the
encoding actually used.

Contexts: user_help, agent_report, pasted_config, log_excerpt, handoff_note,
code_review.

Negative families (20, 60 items each): git_sha, sha256_digest, other_hashes,
uuid_ids, base64_prose, data_uri, tracking_ids, minified_js, lockfile_hash,
doc_example_key, placeholder, public_key_pem, ssh_public_key,
cert_fingerprint, url_query, password_policy_prose, variable_name_no_value,
secret_reference_config, benign_encodings, misc_ids
"""
import base64
import json
import quopri
import random
import re
import string
import struct
import urllib.parse
import uuid

VERSION = "heldout-secrets-gen-1.0.0-2026-11-17"

A_UP = string.ascii_uppercase
A_LO = string.ascii_lowercase
DIG = string.digits
ALNUM = A_UP + A_LO + DIG
B64 = ALNUM + "+/"
B64U = ALNUM + "-_"
HEX = "0123456789abcdef"
B32 = A_UP + "234567"
PWALPHA = ALNUM + "!@#%^*-_+=.~"

N_POS_PER_FAMILY = 50
N_NEG_PER_FAMILY = 60


def rs(r, alpha, n):
    return "".join(r.choice(alpha) for _ in range(n))


def _b64(b):
    return base64.b64encode(b).decode()


def _b64u(b, pad=False):
    s = base64.urlsafe_b64encode(b).decode()
    return s if pad else s.rstrip("=")


def _wrap(s, n):
    return "\n".join(s[i:i + n] for i in range(0, len(s), n))


def _snake(n):
    return re.sub(r"[^A-Za-z0-9]+", "_", n).strip("_").lower()


def _upper(n):
    return _snake(n).upper()


def _camel(n):
    parts = _snake(n).split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _mix(r, s):
    return "".join(c.upper() if r.random() < 0.5 else c.lower() for c in s)


def _one(s):
    return s.replace("\r", "").replace("\n", "\\n")


def _uuid(r):
    return str(uuid.UUID(int=r.getrandbits(128), version=4))


# --------------------------------------------------------------------------
# Positive family generators. Each returns dict(secret, names, [style], [hdr])
# --------------------------------------------------------------------------

def f_aws_access_key_id(r):
    pre = r.choice(["AKIA", "AKIA", "AKIA", "ASIA", "AROA", "AIDA"])
    return dict(secret=pre + rs(r, B32, 16),
                names=["AWS_ACCESS_KEY_ID", "aws_access_key_id", "accessKeyId"],
                style="post")


def f_aws_secret_access_key(r):
    return dict(secret=rs(r, B64, 40),
                names=["AWS_SECRET_ACCESS_KEY", "aws_secret_access_key", "secretAccessKey"],
                style="post")


def f_github_token(r):
    pre = r.choice(["ghp_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_"])
    return dict(secret=pre + rs(r, ALNUM, 36),
                names=["GITHUB_TOKEN", "GH_TOKEN", "github_token", "token"],
                style="hdr", hdr=("Authorization", r.choice(["Bearer ", "token "])))


def f_github_fine_grained_pat(r):
    return dict(secret="github_pat_" + rs(r, ALNUM, 22) + "_" + rs(r, ALNUM, 59),
                names=["GITHUB_PAT", "GH_TOKEN", "github_pat"],
                style="hdr", hdr=("Authorization", "Bearer "))


def f_gitlab_pat(r):
    return dict(secret="glpat-" + rs(r, B64U, 20),
                names=["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN", "gitlab_pat"],
                style="hdr", hdr=("PRIVATE-TOKEN", ""))


def f_slack_token(r):
    k = r.randrange(4)
    if k == 0:
        s = "xoxp-%s-%s-%s-%s" % (rs(r, DIG, 12), rs(r, DIG, 12), rs(r, DIG, 13), rs(r, HEX, 32))
    elif k == 1:
        s = "xapp-1-A%s-%s-%s" % (rs(r, A_UP + DIG, 10), rs(r, DIG, 13), rs(r, HEX, 64))
    else:
        s = "xoxb-%s-%s-%s" % (rs(r, DIG, 12), rs(r, DIG, 13), rs(r, ALNUM, 24))
    return dict(secret=s, names=["SLACK_BOT_TOKEN", "SLACK_TOKEN", "slack_token"],
                style="hdr", hdr=("Authorization", "Bearer "))


def f_slack_webhook_url(r):
    s = "https://hooks.slack.com/services/T%s/B%s/%s" % (
        rs(r, A_UP + DIG, 10), rs(r, A_UP + DIG, 10), rs(r, ALNUM, 24))
    return dict(secret=s, names=["SLACK_WEBHOOK_URL", "webhook_url", "SLACK_HOOK"], style="url")


def f_discord_bot_token(r):
    if r.random() < 0.2:
        s = "https://discord.com/api/webhooks/%s/%s" % (rs(r, DIG, 18), rs(r, B64U, 68))
        return dict(secret=s, names=["DISCORD_WEBHOOK_URL", "discord_webhook"], style="url")
    uid = (r.choice("123456789") + rs(r, DIG, 17)).encode()
    s = "%s.%s.%s" % (_b64(uid).rstrip("="), rs(r, B64U, 6), rs(r, B64U, r.choice([27, 27, 38])))
    return dict(secret=s, names=["DISCORD_TOKEN", "DISCORD_BOT_TOKEN", "bot_token"],
                style="hdr", hdr=("Authorization", "Bot "))


def f_telegram_bot_token(r):
    s = "%s:AA%s" % (rs(r, DIG, r.choice([9, 10])), rs(r, B64U, 33))
    return dict(secret=s, names=["TELEGRAM_BOT_TOKEN", "TG_TOKEN", "bot_token"], style="telegram")


def f_stripe_live_key(r):
    pre = r.choice(["sk_live_", "sk_live_", "rk_live_"])
    return dict(secret=pre + rs(r, ALNUM, r.choice([24, 24, 99])),
                names=["STRIPE_SECRET_KEY", "STRIPE_API_KEY", "stripe_key"], style="user")


def f_openai_api_key(r):
    k = r.randrange(3)
    if k == 0:
        s = "sk-" + rs(r, ALNUM, 20) + "T3BlbkFJ" + rs(r, ALNUM, 20)
    elif k == 1:
        s = "sk-proj-" + rs(r, B64U, r.choice([58, 74])) + "T3BlbkFJ" + rs(r, B64U, r.choice([58, 74]))
    else:
        s = "sk-svcacct-" + rs(r, B64U, 60) + "T3BlbkFJ" + rs(r, B64U, 60)
    return dict(secret=s, names=["OPENAI_API_KEY", "openai_api_key", "OPENAI_KEY"],
                style="hdr", hdr=("Authorization", "Bearer "))


def f_anthropic_api_key(r):
    pre = r.choice(["sk-ant-api03-", "sk-ant-api03-", "sk-ant-admin01-"])
    return dict(secret=pre + rs(r, B64U, 93) + "AA",
                names=["ANTHROPIC_API_KEY", "anthropic_api_key", "CLAUDE_KEY"],
                style="hdr", hdr=("x-api-key", ""))


def f_other_llm_key(r):
    k = r.randrange(6)
    if k == 0:
        s = "hf_" + rs(r, A_UP + A_LO, 34)
        n = ["HF_TOKEN", "HUGGINGFACE_API_KEY"]
    elif k == 1:
        s = "gsk_" + rs(r, ALNUM, 52)
        n = ["GROQ_API_KEY", "groq_key"]
    elif k == 2:
        s = "xai-" + rs(r, ALNUM, 80)
        n = ["XAI_API_KEY", "xai_key"]
    elif k == 3:
        s = "r8_" + rs(r, ALNUM, 37)
        n = ["REPLICATE_API_TOKEN"]
    elif k == 4:
        s = "pplx-" + rs(r, ALNUM, 48)
        n = ["PERPLEXITY_API_KEY"]
    else:
        s = "sk-or-v1-" + rs(r, HEX, 64)
        n = ["OPENROUTER_API_KEY"]
    return dict(secret=s, names=n, style="hdr", hdr=("Authorization", "Bearer "))


def f_google_api_key(r):
    k = r.randrange(5)
    if k == 0:
        s = "ya29." + rs(r, B64U, 110)
        n = ["GOOGLE_ACCESS_TOKEN", "access_token"]
    elif k == 1:
        s = "GOCSPX-" + rs(r, B64U, 28)
        n = ["GOOGLE_CLIENT_SECRET", "client_secret"]
    else:
        s = "AIza" + rs(r, B64U, 35)
        n = ["GOOGLE_API_KEY", "MAPS_API_KEY", "FIREBASE_API_KEY"]
    return dict(secret=s, names=n, style="hdr", hdr=("X-Goog-Api-Key", ""))


def f_sendgrid_api_key(r):
    return dict(secret="SG." + rs(r, B64U, 22) + "." + rs(r, B64U, 43),
                names=["SENDGRID_API_KEY", "sendgrid_key", "SMTP_PASSWORD"],
                style="hdr", hdr=("Authorization", "Bearer "))


def f_jwt(r):
    alg, sig_n = r.choice([("HS256", 32), ("HS256", 32), ("HS512", 64), ("RS256", 256)])
    hdr = {"alg": alg, "typ": "JWT"}
    if r.random() < 0.3:
        hdr["kid"] = rs(r, ALNUM, 12)
    iat = 1760000000 + r.randrange(20000000)
    pl = {"sub": rs(r, DIG, r.choice([10, 18])),
          "iss": r.choice(["https://auth.example.invalid/", "api.example.invalid", "idp.corp.invalid"]),
          "iat": iat, "exp": iat + r.choice([900, 3600, 86400]),
          "jti": _uuid(r)}
    if r.random() < 0.6:
        pl["email"] = r.choice(["dana", "lee", "sam", "ola", "nia"]) + "@example.invalid"
    if r.random() < 0.5:
        pl["role"] = r.choice(["admin", "user", "service", "billing"])
    enc = lambda o: _b64u(json.dumps(o, separators=(",", ":")).encode())
    s = enc(hdr) + "." + enc(pl) + "." + _b64u(r.randbytes(sig_n))
    return dict(secret=s, names=["JWT", "ACCESS_TOKEN", "id_token", "SESSION_JWT"],
                style="hdr", hdr=("Authorization", "Bearer "))


def f_pem_private_key(r):
    k = r.randrange(5)
    if k == 0:
        label, pre, n = "RSA PRIVATE KEY", b"\x30\x82\x04\xa4\x02\x01\x00\x02\x82\x01\x01\x00", r.choice([1180, 1180, 600])
        head = ""
    elif k == 1:
        label, pre, n = "EC PRIVATE KEY", b"\x30\x77\x02\x01\x01\x04\x20", r.choice([112, 160])
        head = ""
    elif k == 2:
        label, pre, n = "PRIVATE KEY", b"\x30\x82\x04\xbd\x02\x01\x00\x30\x0d\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01\x05\x00\x04\x82\x04\xa7", r.choice([1190, 300])
        head = ""
    elif k == 3:
        label, pre, n = "ENCRYPTED PRIVATE KEY", b"\x30\x82\x06\x4e\x30\x28\x06\x0a\x2a\x86\x48\x86\xf7\x0d\x01\x0c\x01\x03\x30\x1a", r.choice([1600, 400])
        head = ""
    else:
        label, pre, n = "RSA PRIVATE KEY", b"\x30\x82\x04\xa3\x02\x01\x00\x02\x82\x01\x01\x00", 900
        head = "Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-256-CBC,%s\n\n" % rs(r, "0123456789ABCDEF", 32)
    body = _wrap(_b64(pre + r.randbytes(n)), 64)
    s = "-----BEGIN %s-----\n%s%s\n-----END %s-----" % (label, head, body, label)
    return dict(secret=s, names=["PRIVATE_KEY", "TLS_KEY", "SIGNING_KEY", "private_key"], style="post")


def f_openssh_private_key(r):
    magic = b"openssh-key-v1\x00" + struct.pack(">I", 4) + b"none" + struct.pack(">I", 4) + b"none" + \
        struct.pack(">I", 0) + struct.pack(">I", 1)
    blob = magic + r.randbytes(r.choice([260, 380, 1300, 1700]))
    s = "-----BEGIN OPENSSH PRIVATE KEY-----\n%s\n-----END OPENSSH PRIVATE KEY-----" % _wrap(_b64(blob), 70)
    return dict(secret=s, names=["SSH_PRIVATE_KEY", "DEPLOY_KEY", "id_ed25519", "ssh_key"], style="post")


def f_db_connection_uri(r):
    scheme = r.choice(["postgres", "postgresql", "mysql", "mongodb", "mongodb+srv", "redis", "rediss",
                       "amqp", "amqps", "mssql"])
    user = r.choice(["app", "svc_api", "admin", "reporting", "etl", "billing", "root", "dbuser"])
    pw = rs(r, ALNUM + "-_.~", r.randrange(14, 29))
    host = "%s-%d.%s.example.invalid" % (r.choice(["db", "pg", "primary", "replica", "cache"]), r.randrange(1, 9),
                                         r.choice(["internal", "prod", "eu-west", "corp"]))
    port = {"postgres": 5432, "postgresql": 5432, "mysql": 3306, "mongodb": 27017, "redis": 6379,
            "rediss": 6380, "amqp": 5672, "amqps": 5671, "mssql": 1433}.get(scheme)
    dbn = r.choice(["main", "orders", "analytics", "app_prod", "0", "vhost"])
    if scheme.startswith("redis"):
        s = "%s://%s:%s@%s:%d/0" % (scheme, r.choice(["", "default"]), pw, host, port)
    elif scheme == "mongodb+srv":
        s = "%s://%s:%s@%s/%s?retryWrites=true&w=majority" % (scheme, user, pw, host, dbn)
    else:
        s = "%s://%s:%s@%s:%d/%s" % (scheme, user, pw, host, port, dbn)
    names = {"redis": ["REDIS_URL"], "rediss": ["REDIS_URL"], "mongodb": ["MONGO_URI", "MONGODB_URI"],
             "mongodb+srv": ["MONGO_URI"], "amqp": ["AMQP_URL", "BROKER_URL"], "amqps": ["BROKER_URL"]}.get(
        scheme, ["DATABASE_URL", "DB_URI", "SQLALCHEMY_DATABASE_URI"])
    return dict(secret=s, names=names, style="post")


WORDS = ["sunflower", "Harbor", "falcon", "maple", "Granite", "tiger", "Orchard", "winter", "Copper", "lantern"]


def f_password_assignment(r):
    if r.random() < 0.4:
        s = r.choice(WORDS) + r.choice(["", "-", "_"]) + rs(r, DIG, r.randrange(2, 5)) + r.choice("!@#%^*-_+=.~")
    else:
        s = rs(r, PWALPHA, r.randrange(10, 25))
    return dict(secret=s, names=["password", "passwd", "secret", "db_password", "DB_PASS", "admin_password",
                                 "root_pw", "client_secret", "PASSWORD"], style="post")


def f_http_authorization(r):
    if r.random() < 0.5:
        k = r.randrange(3)
        tok = rs(r, B64U, r.choice([32, 43, 64])) if k == 0 else (rs(r, HEX, 40) if k == 1 else rs(r, ALNUM, 48))
        return dict(secret=tok, names=["Authorization"], style="hdr", hdr=("Authorization", "Bearer "),
                    prefix="Bearer ")
    user = r.choice(["admin", "svc-deploy", "ci-bot", "alice", "metrics", "api_user"])
    pw = rs(r, ALNUM + "!@#%*-_+=.", r.randrange(10, 22))
    return dict(secret=_b64((user + ":" + pw).encode()), names=["Authorization"], style="hdr",
                hdr=("Authorization", "Basic "), prefix="Basic ")


def f_session_cookie(r):
    k = r.randrange(6)
    if k == 0:
        return dict(secret=rs(r, "0123456789ABCDEF", 32), names=["JSESSIONID"], style="cookie")
    if k == 1:
        return dict(secret=rs(r, A_LO + DIG, 26), names=["PHPSESSID"], style="cookie")
    if k == 2:
        return dict(secret="s%3A" + rs(r, ALNUM, 32) + "." + rs(r, B64U, 43), names=["connect.sid"], style="cookie")
    if k == 3:
        return dict(secret=rs(r, ALNUM, 40), names=["laravel_session"], style="cookie")
    if k == 4:
        return dict(secret=_b64u(r.randbytes(r.choice([48, 96]))), names=["session", "sessionid", "sid"], style="cookie")
    return dict(secret=rs(r, HEX, 64), names=["__Host-session", "_app_session"], style="cookie")


def f_package_registry_token(r):
    k = r.randrange(4)
    if k == 0:
        return dict(secret="npm_" + rs(r, ALNUM, 36), names=["NPM_TOKEN", "NODE_AUTH_TOKEN"], style="hdr",
                    hdr=("Authorization", "Bearer "))
    if k == 1:
        return dict(secret="pypi-AgEIcHlwaS5vcmc" + rs(r, B64U, 70), names=["TWINE_PASSWORD", "PYPI_TOKEN"],
                    style="post")
    if k == 2:
        return dict(secret="rubygems_" + rs(r, HEX, 48), names=["GEM_HOST_API_KEY", "RUBYGEMS_API_KEY"],
                    style="hdr", hdr=("Authorization", ""))
    return dict(secret=_uuid(r), names=["NPM_AUTH_TOKEN", "NPM_TOKEN"], style="hdr",
                hdr=("Authorization", "Bearer "))


def f_ci_token(r):
    k = r.randrange(5)
    if k == 0:
        s, n = "CCIPAT_" + rs(r, ALNUM, 22) + "_" + rs(r, HEX, 40), ["CIRCLE_TOKEN", "CIRCLECI_TOKEN"]
    elif k == 1:
        s, n = "bkua_" + rs(r, HEX, 40), ["BUILDKITE_API_TOKEN"]
    elif k == 2:
        s, n = "glrt-" + rs(r, B64U, 20), ["GITLAB_RUNNER_TOKEN", "CI_RUNNER_TOKEN"]
    elif k == 3:
        s, n = "11" + rs(r, HEX, 32), ["JENKINS_API_TOKEN", "JENKINS_TOKEN"]
    else:
        s, n = rs(r, HEX, 40), ["CIRCLE_TOKEN", "TRAVIS_TOKEN"]
    return dict(secret=s, names=n, style="hdr", hdr=("Authorization", "Bearer "))


FAMILIES = [
    ("aws_access_key_id", f_aws_access_key_id),
    ("aws_secret_access_key", f_aws_secret_access_key),
    ("github_token", f_github_token),
    ("github_fine_grained_pat", f_github_fine_grained_pat),
    ("gitlab_pat", f_gitlab_pat),
    ("slack_token", f_slack_token),
    ("slack_webhook_url", f_slack_webhook_url),
    ("discord_bot_token", f_discord_bot_token),
    ("telegram_bot_token", f_telegram_bot_token),
    ("stripe_live_key", f_stripe_live_key),
    ("openai_api_key", f_openai_api_key),
    ("anthropic_api_key", f_anthropic_api_key),
    ("other_llm_key", f_other_llm_key),
    ("google_api_key", f_google_api_key),
    ("sendgrid_api_key", f_sendgrid_api_key),
    ("jwt", f_jwt),
    ("pem_private_key", f_pem_private_key),
    ("openssh_private_key", f_openssh_private_key),
    ("db_connection_uri", f_db_connection_uri),
    ("password_assignment", f_password_assignment),
    ("http_authorization", f_http_authorization),
    ("session_cookie", f_session_cookie),
    ("package_registry_token", f_package_registry_token),
    ("ci_token", f_ci_token),
]

# --------------------------------------------------------------------------
# Encodings
# --------------------------------------------------------------------------

TRANSFORMS = ["plain", "base64", "base64_nopad", "base64url", "base64url_nopad", "hex_lower", "hex_upper",
              "url_encoded", "url_double_encoded", "json_escaped", "json_unicode_escaped", "base64_wrap64",
              "base64_wrap76", "quoted_printable", "html_entity_named", "html_entity_numeric", "reversed",
              "split_concat_literal", "zero_width_inserted", "double_base64", "base64_of_assignment",
              "json_nested_escaped"]
WRAPPERS = ["shell_command", "curl_line", "dotenv", "yaml", "toml", "json_document", "ini", "python_literal",
            "js_literal", "dockerfile_env", "log_line", "stack_trace", "markdown_fence",
            "bracketed_punctuation", "backslash_continuation", "mixed_case_keyword"]
ENCODINGS = TRANSFORMS + WRAPPERS

HUMAN = {
    "plain": "plain", "base64": "base64", "base64_nopad": "base64, no padding", "base64url": "base64url",
    "base64url_nopad": "base64url, unpadded", "hex_lower": "hex", "hex_upper": "uppercase hex",
    "url_encoded": "percent-encoded", "url_double_encoded": "double percent-encoded",
    "json_escaped": "JSON-escaped", "json_unicode_escaped": "JSON unicode-escaped",
    "base64_wrap64": "base64, 64 columns", "base64_wrap76": "base64, 76 columns",
    "quoted_printable": "quoted-printable", "html_entity_named": "HTML entities",
    "html_entity_numeric": "numeric HTML entities", "reversed": "reversed",
    "split_concat_literal": "split string literal", "zero_width_inserted": "zero-width padded",
    "double_base64": "double base64", "base64_of_assignment": "base64 of the whole assignment",
    "json_nested_escaped": "nested JSON string",
}

_NAMED = {"/": "&sol;", "+": "&plus;", "=": "&equals;", "-": "&hyphen;", "_": "&lowbar;", ":": "&colon;",
          ".": "&period;", "@": "&commat;", "#": "&num;", "!": "&excl;", "%": "&percnt;", "^": "&Hat;",
          "*": "&ast;", "~": "&tilde;", "\n": "&NewLine;", "&": "&amp;"}


def _transform(enc, s, name, r):
    b = s.encode()
    if enc == "plain":
        return s
    if enc == "base64":
        out = _b64(b)
    elif enc == "base64_nopad":
        out = _b64(b).rstrip("=")
    elif enc == "base64url":
        out = _b64u(b, pad=True)
    elif enc == "base64url_nopad":
        out = _b64u(b)
    elif enc == "hex_lower":
        out = b.hex()
    elif enc == "hex_upper":
        out = b.hex().upper()
    elif enc == "url_encoded":
        out = urllib.parse.quote(s, safe="")
    elif enc == "url_double_encoded":
        out = urllib.parse.quote(urllib.parse.quote(s, safe=""), safe="")
    elif enc == "json_escaped":
        out = json.dumps(s)[1:-1].replace("/", "\\/")
    elif enc == "json_unicode_escaped":
        out = "".join(("\\u%04x" % ord(c)) if (k == 0 or not c.isalnum() or r.random() < 0.3) else c
                      for k, c in enumerate(s))
    elif enc == "base64_wrap64":
        out = _wrap(_b64(b), 64)
    elif enc == "base64_wrap76":
        out = _wrap(_b64(b), 76)
    elif enc == "quoted_printable":
        out = quopri.encodestring(b, quotetabs=True).decode().rstrip("\n")
    elif enc == "html_entity_named":
        out = "".join(_NAMED.get(c, c) for c in s)
    elif enc == "html_entity_numeric":
        out = "".join(("&#%d;" % ord(c)) if not c.isalnum() else
                      (("&#x%x;" % ord(c)) if r.random() < 0.3 else c) for c in s)
        if out == s:
            out = "&#x%x;" % ord(s[0]) + s[1:]
    elif enc == "reversed":
        out = s[::-1]
    elif enc == "split_concat_literal":
        flat = _one(s)
        parts, i = [], 0
        while i < len(flat):
            step = r.randrange(8, 17)
            parts.append(flat[i:i + step])
            i += step
        out = " + ".join('"%s"' % p for p in parts)
    elif enc == "zero_width_inserted":
        pos = sorted(r.sample(range(1, len(s)), min(4, len(s) - 1)))
        out, last = "", 0
        for p in pos:
            out += s[last:p] + "\u200b"
            last = p
        out += s[last:]
    elif enc == "double_base64":
        out = _b64(_b64(b).encode())
    elif enc == "base64_of_assignment":
        out = _b64(("%s=%s\n" % (_upper(name), s)).encode())
    elif enc == "json_nested_escaped":
        out = json.dumps(json.dumps({_snake(name): s}))
    else:
        raise ValueError(enc)
    return None if out == s else out


def _snip_transform(enc, t, name, meta, r):
    N = _upper(name)
    h = HUMAN[enc]
    if enc == "plain":
        pfx = meta.get("prefix", "")
        return r.choice(["my %s is %s%s" % (name, pfx, t), "%s: %s%s" % (name, pfx, t),
                         "%s=%s%s" % (N, pfx, t)]) if "\n" not in t else \
            r.choice(["%s:\n%s" % (name, t), "here is the %s\n%s" % (name, t)])
    if "\n" in t:
        return r.choice(["%s (%s):\n%s" % (N, h, t), "%s dump of %s:\n%s" % (h, N, t)])
    if enc == "split_concat_literal":
        return r.choice(["%s = %s" % (N, t), "const %s = %s;" % (_camel(name), t)])
    return r.choice(["%s (%s) = %s" % (N, h, t), "%s_%s=%s" % (N, enc.upper(), t),
                     "the %s, %s: %s" % (name, h, t), "echo '%s' # %s" % (t, h)])


# --------------------------------------------------------------------------
# Syntax wrappers
# --------------------------------------------------------------------------

def _ts(r):
    return "2026-11-%02dT%02d:%02d:%02d.%03dZ" % (r.randrange(1, 17), r.randrange(24), r.randrange(60),
                                                  r.randrange(60), r.randrange(1000))


def w_shell_command(n, raw, one, m, r):
    N = _upper(n)
    return r.choice(['export %s="%s"' % (N, one),
                     "%s='%s' ./scripts/deploy.sh --env production" % (N, one),
                     "$ export %s=%s\n$ ./scripts/run-migrations.sh" % (N, one),
                     "env %s='%s' python worker.py --once" % (N, one)])


def w_curl_line(n, raw, one, m, r):
    st = m.get("style", "post")
    if st == "hdr":
        h, p = m.get("hdr", ("Authorization", "Bearer "))
        return 'curl -sS -H "%s: %s%s" https://api.example.invalid/v1/me' % (h, p, one)
    if st == "url":
        return "curl -X POST -H 'Content-type: application/json' --data '{\"text\":\"build finished\"}' %s" % one
    if st == "cookie":
        return 'curl -sS -b "%s=%s" https://app.example.invalid/account' % (n, one)
    if st == "user":
        return "curl -sS https://api.example.invalid/v1/charges -u %s:" % one
    if st == "telegram":
        return "curl -s https://api.telegram.org/bot%s/getMe" % one
    return "curl -sS -X POST -d '%s=%s' https://config.example.invalid/set" % (_snake(n), one)


def w_dotenv(n, raw, one, m, r):
    N = _upper(n)
    q = r.choice(['"', "'", ""])
    return "# production overrides\n%s=%s%s%s\nLOG_LEVEL=info" % (N, q, one, q)


def w_yaml(n, raw, one, m, r):
    k = _snake(n)
    if "\n" in raw:
        body = "\n".join("    " + ln for ln in raw.split("\n"))
        return "service:\n  %s: |\n%s\n  replicas: 2" % (k, body)
    return r.choice(["service:\n  name: billing\n  %s: %s\n  replicas: 2" % (k, one),
                     "env:\n  - name: %s\n    value: \"%s\"" % (_upper(n), one)])


def w_toml(n, raw, one, m, r):
    return '[credentials]\n%s = "%s"\ntimeout = 30' % (_snake(n), one)


def w_json_document(n, raw, one, m, r):
    return json.dumps({"service": "billing-worker", "env": "prod", _snake(n): raw}, indent=2)


def w_ini(n, raw, one, m, r):
    return "[default]\nregion = eu-west-1\n%s = %s" % (_snake(n), one)


def w_python_literal(n, raw, one, m, r):
    return r.choice(['%s = "%s"' % (_upper(n), one),
                     'config = {"%s": "%s", "retries": 3}' % (_snake(n), one)])


def w_js_literal(n, raw, one, m, r):
    return r.choice(["const %s = '%s';" % (_camel(n), one),
                     'module.exports = { %s: "%s", timeout: 5000 };' % (_camel(n), one)])


def w_dockerfile_env(n, raw, one, m, r):
    return r.choice(["FROM python:3.12-slim\nENV %s=%s\nCOPY . /app" % (_upper(n), one),
                     "ARG %s=%s\nRUN ./build.sh" % (_upper(n), one)])


def w_log_line(n, raw, one, m, r):
    return "%s %s [%s] retrying with credentials %s=%s" % (
        _ts(r), r.choice(["INFO", "WARN", "DEBUG", "ERROR"]),
        r.choice(["deploy", "auth-client", "worker-7", "sync"]), _upper(n), one)


def w_stack_trace(n, raw, one, m, r):
    if r.random() < 0.5:
        return ('Traceback (most recent call last):\n  File "/app/client.py", line %d, in connect\n'
                '    session = login(%s="%s")\nConnectionError: 401 Unauthorized' % (r.randrange(20, 400), _snake(n), one))
    return ("Error: request failed with status 403\n    at Client.request (/srv/app/lib/client.js:%d:%d)\n"
            "    at async main (/srv/app/index.js:12:5) {\n  config: { %s: '%s' }\n}" % (
                r.randrange(20, 300), r.randrange(3, 40), _camel(n), one))


def w_markdown_fence(n, raw, one, m, r):
    return "```%s\n%s=%s\n```" % (r.choice(["env", "ini", "text", "bash"]), _upper(n), raw)


def w_bracketed_punctuation(n, raw, one, m, r):
    o, c = r.choice([("(", ")"), ("[", "]"), ("<", ">"), ('"', '"'), ("'", "'"), ("`", "`"), ("{", "}")])
    return "the %s is %s%s%s%s" % (n, o, one, c, r.choice([".", ",", ";", " -- please rotate it"]))


def w_backslash_continuation(n, raw, one, m, r):
    cut = r.randrange(max(2, len(one) // 4), max(3, 3 * len(one) // 4))
    return "export %s=%s\\\n%s" % (_upper(n), one[:cut], one[cut:])


def w_mixed_case_keyword(n, raw, one, m, r):
    if m.get("prefix"):
        return "%s: %s%s" % (_mix(r, "Authorization"), _mix(r, m["prefix"]), one)
    if m.get("style") == "cookie":
        return "%s: %s=%s" % (_mix(r, "Cookie"), n, one)
    return "%s=%s" % (_mix(r, _upper(n)), one)


WRAP_FN = {k: globals()["w_" + k] for k in WRAPPERS}

# --------------------------------------------------------------------------
# Context prose
# --------------------------------------------------------------------------

CONTEXTS = {
    "user_help": [
        ("hey, I keep getting a 401 from the service and I can't see why. This is what I have:\n",
         "\ncould it be a typo somewhere?"),
        ("Can you help me debug this? It worked yesterday.\n", "\nNothing else changed as far as I know."),
        ("I'm setting this up on a new laptop, does this look right?\n", "\nThanks!"),
        ("quick question about our integration, the relevant bit is\n", "\nwhy would it be rejected?"),
    ],
    "agent_report": [
        ("I ran the deploy step and this is what the config looked like:\n", "\nThe command exited with status 1."),
        ("Ran cat on the settings file on the build host. Output:\n", "\nNext I'll check the service logs."),
        ("While reproducing the failure I captured the following:\n", "\nI have not changed anything yet."),
        ("Step 3 of the checklist printed:\n", "\nMoving on to the smoke test."),
    ],
    "pasted_config": [
        ("Pasted from my repo:\n", "\n"),
        ("this is our current setup file\n", "\n(trimmed for length)"),
        ("config as it is on staging right now\n", "\nrest of the file is defaults."),
        ("here's the whole block:\n\n", "\n\nend of block"),
    ],
    "log_excerpt": [
        ("Excerpt from the CI log:\n", "\n... 14 more lines"),
        ("journalctl -u worker | tail shows\n", "\n-- Logs end --"),
        ("From the pod logs just before the crash:\n", "\ncontainer restarted"),
        ("grep output for the failing job:\n", "\n(3 matches)"),
    ],
    "handoff_note": [
        ("Handoff: the integration lives in the infra repo. Current values:\n", "\nOwner for this is the platform team."),
        ("Notes for whoever picks this up next:\n", "\nTicket is linked in the tracker."),
        ("Summary of what I found, details below.\n", "\nSee the runbook for rollback."),
        ("FYI for the on-call, copied from the incident doc:\n", "\nPostmortem is scheduled."),
    ],
    "code_review": [
        ("Reviewing the diff, line 42 looks off:\n", "\nShould this be in a vault instead?"),
        ("In the PR I see this change:\n", "\nI'd like another opinion before approving."),
        ("this hunk is the one I'm worried about\n", "\nthe tests pass locally though."),
        ("Nit on the new file:\n", "\nOtherwise LGTM."),
    ],
}
CTX_NAMES = sorted(CONTEXTS)
_PREF_CTX = {"log_line": "log_excerpt", "stack_trace": "log_excerpt", "dotenv": "pasted_config",
             "yaml": "pasted_config", "toml": "pasted_config", "ini": "pasted_config",
             "json_document": "pasted_config", "dockerfile_env": "pasted_config",
             "markdown_fence": "code_review", "python_literal": "code_review", "js_literal": "code_review",
             "shell_command": "agent_report", "curl_line": "agent_report"}


def _pick_ctx(r, enc):
    pref = _PREF_CTX.get(enc)
    if pref and r.random() < 0.7:
        return pref
    return r.choice(CTX_NAMES)


def _frame(r, ctx, snippet):
    before, after = r.choice(CONTEXTS[ctx])
    return before + snippet + after


# --------------------------------------------------------------------------
# Positives
# --------------------------------------------------------------------------

def _make_positive(seed, fi, fname, gen, i):
    r = random.Random(seed * 1000003 + fi * 10007 + i)
    meta = gen(r)
    secret = meta["secret"]
    name = r.choice(meta["names"])
    start = (i + fi * 5) % len(ENCODINGS)
    for k in range(len(ENCODINGS)):
        enc = ENCODINGS[(start + k) % len(ENCODINGS)]
        if enc in WRAPPERS:
            raw = meta.get("prefix", "") + secret
            snippet = WRAP_FN[enc](name, raw, _one(raw), meta, r)
            break
        t = _transform(enc, secret, name, r)
        if t is None:
            continue
        snippet = _snip_transform(enc, t, name, meta, r)
        break
    ctx = _pick_ctx(r, enc)
    return {"family": fname, "encoding": enc, "context": ctx, "text": _frame(r, ctx, snippet), "secret": secret}


def positives(seed: int = 16096) -> list:
    out = []
    for fi, (fname, gen) in enumerate(FAMILIES):
        for i in range(N_POS_PER_FAMILY):
            out.append(_make_positive(seed, fi, fname, gen, i))
    random.Random(seed ^ 0x5eed).shuffle(out)
    return out


# --------------------------------------------------------------------------
# Negatives
# --------------------------------------------------------------------------

SENTENCES = ["The quarterly report is attached, please review before Friday.",
             "Remember to water the plants and take out the recycling.",
             "Our meeting is moved to the small conference room on the second floor.",
             "The quick brown fox jumps over the lazy dog.",
             "Please find the updated agenda for next week's planning session.",
             "Lunch is on the team today, vegetarian options are available.",
             "The deployment window opens at nine and closes at noon.",
             "Thanks for the quick turnaround on the design mockups."]


def n_git_sha(r):
    sha = rs(r, HEX, 40)
    return r.choice(["commit %s\nAuthor: Dana <dana@example.invalid>" % sha,
                     "git checkout %s" % sha,
                     "%s Merge pull request #%d from feature/%s" % (sha[:7], r.randrange(10, 9000), r.choice(["login", "cache", "ui-fix"])),
                     "Reviewed HEAD %s, delivered commit %s" % (sha, rs(r, HEX, 40)),
                     "git cherry-pick %s %s" % (sha[:10], rs(r, HEX, 10))])


def n_sha256_digest(r):
    h = rs(r, HEX, 64)
    return r.choice(["%s  release-%d.%d.tar.gz" % (h, r.randrange(1, 9), r.randrange(0, 30)),
                     "Digest: sha256:%s" % h,
                     "Packet SHA-256: %s" % h,
                     "image: registry.example.invalid/app@sha256:%s" % h])


def n_other_hashes(r):
    return r.choice(["md5sum: %s  backup.sql" % rs(r, HEX, 32),
                     "sha1: %s" % rs(r, HEX, 40),
                     "sha512: %s" % rs(r, HEX, 128),
                     "ETag: \"%s\"" % rs(r, HEX, 32),
                     "checksum (crc32) %s" % rs(r, HEX, 8)])


def n_uuid_ids(r):
    u = _uuid(r)
    return r.choice(["request_id=%s" % u, "trace-id: %s" % u, "https://app.example.invalid/orders/%s" % u,
                     '{"id": "%s", "status": "ok"}' % u, "correlation id %s was logged twice" % u])


def n_base64_prose(r):
    s = "%s Room %d." % (r.choice(SENTENCES), r.randrange(100, 999))
    b = _b64(s.encode())
    return r.choice(["echo '%s' | base64 -d" % b, "Content-Transfer-Encoding: base64\n\n%s" % b,
                     "note (base64): %s" % b, "body: %s" % _b64u(s.encode())])


_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
_GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x01D\x00;"


def n_data_uri(r):
    if r.random() < 0.5:
        d = "data:image/png;base64," + _b64(_PNG + r.randbytes(r.randrange(24, 60)))
    elif r.random() < 0.5:
        d = "data:image/gif;base64," + _b64(_GIF)
    else:
        d = "data:image/svg+xml;utf8," + urllib.parse.quote('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d"><rect width="100%%" height="100%%" fill="#%s"/></svg>' % (r.randrange(8, 64), r.randrange(8, 64), rs(r, HEX, 6)))
    return r.choice(['<img src="%s" alt="icon">' % d, "background-image: url(%s);" % d, d])


def n_tracking_ids(r):
    return r.choice(["Order ORD-2026-%s is out for delivery" % rs(r, DIG, 8),
                     "UPS tracking 1Z%s" % rs(r, A_UP + DIG, 16),
                     "USPS tracking %s" % rs(r, DIG, 22),
                     "Invoice INV-%s-%s" % (rs(r, DIG, 4), rs(r, A_UP + DIG, 6)),
                     "ASIN B0%s" % rs(r, A_UP + DIG, 8),
                     "Ticket PLAT-%d is blocked on the vendor" % r.randrange(100, 9999),
                     "Case # %s-%s" % (rs(r, DIG, 5), rs(r, DIG, 5))])


_JS = ['!function(e,t){"use strict";var n=e.document,r=t.length;', "function a(e){return e&&e.__esModule?e:{default:e}}",
       "var o=Object.keys(i).map(function(e){return[e,i[e]]});", 'window.__INITIAL_STATE__={"user":null,"flags":{"beta":!1}};',
       'Object.defineProperty(t,"__esModule",{value:!0});', "e.exports=n(7).Promise;",
       'document.addEventListener("DOMContentLoaded",function(){init()});']


def n_minified_js(r):
    parts = r.sample(_JS, 3)
    parts.append('n.p+"static/js/"+({}[e]||e)+"."+{1:"%s",2:"%s"}[e]+".chunk.js"' % (rs(r, HEX, 16), rs(r, HEX, 16)))
    parts.append('var u="%s";' % rs(r, ALNUM, 22))
    return "".join(parts) + "//# sourceMappingURL=main.%s.js.map" % rs(r, HEX, 8)


def n_lockfile_hash(r):
    return r.choice(['"integrity": "sha512-%s",' % _b64(r.randbytes(64)),
                     "integrity sha1-%s" % _b64(r.randbytes(20)),
                     "github.com/example/lib v1.%d.0 h1:%s" % (r.randrange(30), _b64(r.randbytes(32))),
                     'checksum = "%s"' % rs(r, HEX, 64),
                     '{ file = "pkg-1.%d.whl", hash = "sha256:%s" }' % (r.randrange(30), rs(r, HEX, 64))])


def n_doc_example_key(r):
    aws_id = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    aws_sk = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLE" + "KEY"
    stripe = "sk_test_" + "4eC39HqLyjWDarj" + "tT1zdp7dc"
    hdr = _b64u(b'{"alg":"HS256","typ":"JWT"}')
    pl = _b64u(b'{"sub":"1234567890","name":"John Doe","iat":1516239022}')
    # public documentation example: the default HS256 sample token shown on https://jwt.io (negative set only)
    jwt_doc = hdr + "." + pl + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    mongo = "mongodb://myDBReader:D1fficultP%40ssw0rd@mongodb0.example.com:27017/?authSource=admin"
    pick = r.choice([
        "aws_access_key_id = %s" % aws_id,
        "aws_secret_access_key = %s" % aws_sk,
        "export AWS_ACCESS_KEY_ID=%s\nexport AWS_SECRET_ACCESS_KEY=%s" % (aws_id, aws_sk),
        "curl https://api.stripe.com/v1/charges -u %s:" % stripe,
        "jwt.io sample: %s" % jwt_doc,
        "mongosh \"%s\"" % mongo,
        "psql postgres://user:password@localhost:5432/mydb",
        "SLACK_BOT_TOKEN=xoxb-your-token-here",
        "GITHUB_TOKEN=ghp_" + "x" * 36,
        "Authorization: Bearer YOUR_ACCESS_TOKEN",
    ])
    return "From the vendor docs (ticket DOC-%d): %s" % (r.randrange(1000, 9999), pick)


def n_placeholder(r):
    nm = r.choice(["API_KEY", "DB_PASSWORD", "STRIPE_SECRET_KEY", "AUTH_TOKEN", "SECRET_KEY", "CLIENT_SECRET"])
    return r.choice(["%s=${%s}" % (nm, nm), "%s=<your-%s>" % (nm, nm.lower().replace("_", "-")),
                     "%s=xxxxxxxxxxxxxxxx" % nm, "%s=changeme" % nm, "%s=YOUR_%s_HERE" % (nm, nm),
                     "%s: {{ secrets.%s }}" % (nm.lower(), nm), "%s=$(cat /run/secrets/%s)" % (nm, nm.lower()),
                     'key = os.environ["%s"]' % nm, "token: ${{ secrets.%s }}" % nm,
                     "%s=sk-%s" % (nm, "x" * 48), "password = '********'", "%s=REPLACE_ME" % nm])


def n_public_key_pem(r):
    label = r.choice(["PUBLIC KEY", "RSA PUBLIC KEY", "CERTIFICATE", "CERTIFICATE REQUEST"])
    pre = b"\x30\x82\x01\x22\x30\x0d\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01\x05\x00\x03\x82\x01\x0f\x00"
    body = _wrap(_b64(pre + r.randbytes(r.choice([260, 420]))), 64)
    return "-----BEGIN %s-----\n%s\n-----END %s-----" % (label, body, label)


def n_ssh_public_key(r):
    who = r.choice(["dana@laptop", "deploy@ci", "eric@workstation"])
    if r.random() < 0.5:
        blob = b"\x00\x00\x00\x07ssh-rsa\x00\x00\x00\x03\x01\x00\x01\x00\x00\x01\x01\x00" + r.randbytes(256)
        return "ssh-rsa %s %s" % (_b64(blob), who)
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + r.randbytes(32)
    return "ssh-ed25519 %s %s" % (_b64(blob), who)


def n_cert_fingerprint(r):
    if r.random() < 0.5:
        return "SHA256 Fingerprint: %s" % ":".join("%02X" % x for x in r.randbytes(32))
    return r.choice(["SHA256:%s" % _b64(r.randbytes(32)).rstrip("="),
                     "SHA1 Fingerprint=%s" % ":".join("%02X" % x for x in r.randbytes(20))])


def n_url_query(r):
    return "https://shop.example.invalid/search?q=%s&page=%d&sort=%s&utm_source=%s&ref=%s" % (
        r.choice(["blue+shoes", "usb+cable", "garden+hose"]), r.randrange(1, 40),
        r.choice(["price", "rating", "newest"]), r.choice(["newsletter", "twitter", "partner"]), rs(r, HEX, 7))


def n_password_policy_prose(r):
    return r.choice(["Your password must be at least %d characters and include a number." % r.randrange(8, 17),
                     "Passwords expire every %d days; you cannot reuse your last %d passwords." % (r.choice([60, 90]), r.randrange(3, 12)),
                     "Choose a strong passphrase. Do not share your password or API token with anyone.",
                     "Password requirements: %d+ chars, one uppercase, one symbol." % r.randrange(8, 17),
                     "If you forgot your password, use the reset link; support will never ask for it."])


def n_variable_name_no_value(r):
    return r.choice(['api_key = os.environ.get("API_KEY")', "const apiToken = process.env.API_TOKEN;",
                     'password: ""', "PASSWORD=", "secret_key: ${SECRET_KEY}", "def get_api_key(self): ...",
                     "Authorization: Bearer <token>", "password: ********", "token = None  # set at runtime",
                     "private_key_path = /etc/ssl/private/app.key", "aws_secret_access_key = <redacted>"])


def n_secret_reference_config(r):
    return r.choice(["password_min_length: %d\ntoken_ttl_seconds: %d" % (r.randrange(8, 17), r.choice([900, 3600])),
                     "api_key_header: X-Api-Key\napi_key_required: true",
                     "secret_name: prod/db-credentials\nsecret_version: %d" % r.randrange(1, 9),
                     "env:\n  - name: DB_PASSWORD\n    valueFrom:\n      secretKeyRef:\n        name: db-credentials\n        key: password",
                     "vault_path = \"secret/data/app/%s\"" % r.choice(["db", "stripe", "smtp"]),
                     "auth:\n  method: oauth2\n  client_id_ref: ssm:/app/client_id"])


def n_benign_encodings(r):
    s = r.choice(["hello world", "caf\u00e9 menu", "<div class=\"box\">", "a&b=c", "meeting at 10:30"])
    return r.choice(["q=%s" % urllib.parse.quote(s), "hex: %s" % s.encode().hex(),
                     "html: %s" % s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"),
                     "json: %s" % json.dumps(s), "qp: %s" % quopri.encodestring(s.encode()).decode().strip(),
                     "text/plain; charset=utf-8 body %s" % _b64(s.encode())])


def n_misc_ids(r):
    return r.choice(["api-%s-%s" % (rs(r, HEX + "g", 9), rs(r, A_LO + DIG, 5)),
                     "v%d.%d.%d-rc.%d+build.%d" % (r.randrange(9), r.randrange(30), r.randrange(30), r.randrange(9), r.randrange(999)),
                     "color: #%s; border: 1px solid #%s;" % (rs(r, HEX, 6), rs(r, HEX, 6)),
                     "mac %s" % ":".join(rs(r, HEX, 2) for _ in range(6)),
                     "main.%s.chunk.css" % rs(r, HEX, 8),
                     "S3 version id %s" % rs(r, ALNUM + "._", 32),
                     "ETag: W/\"%s\"" % rs(r, HEX, 16),
                     "build %s finished in %dms" % (rs(r, ALNUM, 12), r.randrange(100, 90000))])


NEG_FAMILIES = [
    ("git_sha", n_git_sha), ("sha256_digest", n_sha256_digest), ("other_hashes", n_other_hashes),
    ("uuid_ids", n_uuid_ids), ("base64_prose", n_base64_prose), ("data_uri", n_data_uri),
    ("tracking_ids", n_tracking_ids), ("minified_js", n_minified_js), ("lockfile_hash", n_lockfile_hash),
    ("doc_example_key", n_doc_example_key), ("placeholder", n_placeholder), ("public_key_pem", n_public_key_pem),
    ("ssh_public_key", n_ssh_public_key), ("cert_fingerprint", n_cert_fingerprint), ("url_query", n_url_query),
    ("password_policy_prose", n_password_policy_prose), ("variable_name_no_value", n_variable_name_no_value),
    ("secret_reference_config", n_secret_reference_config), ("benign_encodings", n_benign_encodings),
    ("misc_ids", n_misc_ids),
]


def negatives(seed: int = 16096) -> list:
    out = []
    for fi, (fname, gen) in enumerate(NEG_FAMILIES):
        for i in range(N_NEG_PER_FAMILY):
            r = random.Random(seed * 999983 + fi * 20011 + i + 7)
            snippet = gen(r)
            ctx = r.choice(CTX_NAMES)
            out.append({"family": fname, "context": ctx, "text": _frame(r, ctx, snippet)})
    random.Random(seed ^ 0xbad).shuffle(out)
    return out


def describe() -> dict:
    pos, neg = positives(), negatives()

    def count(items, key):
        d = {}
        for it in items:
            d[it[key]] = d.get(it[key], 0) + 1
        return dict(sorted(d.items()))

    fam_per_enc = {}
    for it in pos:
        fam_per_enc.setdefault(it["encoding"], set()).add(it["family"])
    return {
        "version": VERSION,
        "positives": {"total": len(pos), "by_family": count(pos, "family"),
                      "by_encoding": count(pos, "encoding"), "by_context": count(pos, "context"),
                      "families_per_encoding": {k: len(v) for k, v in sorted(fam_per_enc.items())}},
        "negatives": {"total": len(neg), "by_family": count(neg, "family"), "by_context": count(neg, "context")},
    }


if __name__ == "__main__":
    print(json.dumps(describe(), indent=1))
