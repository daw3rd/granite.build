#!/usr/bin/env python3

# Copyright LLM.build Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Redaction of secret-looking values before they leave the server.

Used when emitting step ``config``/``metadata`` into build lineage, which is readable
by any space member. Two complementary passes run:

* **Key-name masking** (:data:`SENSITIVE_KEY_RE`) — replaces the value of any key whose
  *name* looks secret (``password``, ``token``, ...). A secret under a non-secret-looking
  key is not caught by this pass alone — an accepted defense-in-depth tradeoff, since step
  metadata is largely operational data (e.g. a git commit SHA).
* **Value scrubbing** (:func:`scrub_url_credentials`) — masks ``userinfo@`` credentials
  embedded in URL-shaped strings regardless of their key name. This closes the specific
  leak of a credentialed clone URL (e.g. ``https://x-access-token:ghp_x@github.com/org/repo``)
  riding in a byoc/BYOS step config or ``step_uri`` under an innocuous key like ``repo``.

Strings get a third pass, since a secret can ride inside free text (a command line,
or a workload's log tail attached to a failure event) where no key isolates it:
``NAME=value`` / ``name: value`` assignments whose name matches
:data:`SENSITIVE_KEY_RE`, ``Authorization``-style header values, and well-known token
shapes (``hf_…``, ``ghp_…``, ``AKIA…``) are masked too. :func:`sanitize_log_tail`
wraps that with the fence/size handling both the LSF and SkyPilot failure paths need.
"""

import re
from typing import Any, Iterable, Optional

__all__ = [
    "SENSITIVE_KEY_RE",
    "URL_USERINFO_RE",
    "REDACTED",
    "LOG_TAIL_MAX_BYTES",
    "redact_sensitive",
    "sanitize_log_tail",
    "scrub_url_credentials",
]

# Case-insensitive, ``-``/``_`` tolerant match on common secret-bearing key names.
# Verbose form (whitespace/comments ignored) for readability; each alternative is a
# substring test against the key name. ``[_-]?`` also tolerates camelCase because the
# separator is optional and the following letter is matched case-insensitively (so
# ``apiKey``/``sshKey`` match too).
SENSITIVE_KEY_RE = re.compile(
    r"""
      password | passwd | pwd
    | secret
    | token
    | credential
    | cookie
    | bearer
    | api[_-]?key
    | access[_-]?key
    | private[_-]?key
    | ssh[_-]?key
    | authorization
    | auth(?!or|en)          # 'auth', 'auth_token', 'authToken' — but NOT 'author'/'authentic…'
                             # Exclusion is on the word stem (case-insensitive via re.IGNORECASE),
                             # so all-caps keys like AUTHOR / AUTHORED_DATE / GIT_AUTHOR are also
                             # left unmasked (the old (?-i:[a-z]) guard only skipped lowercase).
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Placeholder substituted for the value of any secret-looking key.
REDACTED = "<redacted>"

# Matches the ``userinfo`` (credential) component of a URL: a scheme, ``://``, then a
# ``user:secret`` pair up to the first ``@``. The embedded ``:`` is required, so only a
# credential *pair* is masked — a bare ``user@``/``git@`` (a username with no password)
# is left intact. Neither side may contain ``/`` or whitespace, so the match stays within
# the authority component and never crosses into the path (``https://github.com/a@b`` is
# left alone). Captures the ``scheme://`` prefix so it can be preserved while the
# credentials are replaced. Works standalone or embedded in a larger command string.
# Note: this does NOT catch a token-as-username with no ``:`` (``https://TOKEN@host``);
# such secrets should be stored under a secret-*named* key so key-name masking covers them.
URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]*:[^/@\s]*@")


def scrub_url_credentials(text: str) -> str:
    """Mask ``userinfo@`` credentials embedded in any URL-shaped substrings.

    Value-level companion to the key-name redaction: catches a secret that rides
    inside a URL *value* (e.g. a credentialed git clone URL) under a key whose name
    does not look sensitive. The scheme, host, and path are preserved so the entry
    stays useful in lineage; only the credential portion is replaced.

    Only a ``user:secret`` pair is masked; a bare ``user@``/``git@`` (username with
    no password) is preserved. A token-as-username with no ``:`` is therefore not
    caught here — rely on a secret-named key for that case.

    :param text: an arbitrary string, possibly containing one or more URLs
        (standalone or embedded in a larger command line).
    :returns: the string with each URL's ``user:secret`` credential pair replaced by
        ``REDACTED`` (``scheme://<redacted>@host/...``); a string with no
        credential-pair URL is returned unchanged.
    """
    return URL_USERINFO_RE.sub(rf"\1{REDACTED}@", text)


# Well-known credential shapes, masked wherever they appear in free text. Prefix-based,
# so ordinary log content (hashes, UUIDs, paths) is left alone.
_TOKEN_RE = re.compile(
    r"""
      \bhf_[A-Za-z0-9]{20,}                       # Hugging Face
    | \bgh[pousr]_[A-Za-z0-9]{20,}                # GitHub classic tokens
    | \bgithub_pat_[A-Za-z0-9_]{20,}              # GitHub fine-grained PAT
    | \b(?:AKIA|ASIA)[A-Z0-9]{16}\b               # AWS access key id
    | \bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}      # OpenAI / Anthropic style keys
    | \bxox[abposr]-[A-Za-z0-9-]{10,}             # Slack
    | \beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+  # JWT
    """,
    re.VERBOSE,
)

# ``Authorization: Bearer <x>`` / ``Basic <x>`` header values.
_AUTH_HEADER_RE = re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}")

# ``NAME=value`` / ``name: value`` assignments whose *name* looks secret (same key-name
# test as :data:`SENSITIVE_KEY_RE`, allowing a prefix such as ``HF_``/``AWS_``). The
# value runs to whitespace or a closing quote; group 1 keeps the name and separator.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"""
    (\b[A-Za-z0-9_.-]*?(?:"""
    + SENSITIVE_KEY_RE.pattern
    + r""")[A-Za-z0-9_.-]*["']?\s*[:=]\s*["']?)   # key, separator, opening quote
    [^\s"',;]{3,}                                  # the value
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A declared secret value shorter than this is not masked verbatim: it would also hit
# ordinary words (a secret "the" must not blank out every "the" in the log).
_MIN_SECRET_VALUE_LEN = 6


def _redact_string(text: str, secret_values: Iterable[str]) -> str:
    """Mask secrets inside one string (see :func:`redact_sensitive`).

    Most specific first: URL ``user:secret@`` credentials
    (:func:`scrub_url_credentials`, first so the assignment pass can't mistake
    ``x-access-token:...@`` for a ``name: value`` pair and mangle the URL), the
    known ``secret_values`` verbatim, well-known token shapes anywhere,
    ``Authorization``-style header values, then ``NAME=value`` assignments whose
    name matches :data:`SENSITIVE_KEY_RE`.

    :param text: the string to redact.
    :param secret_values: exact secret strings to mask; values shorter than
        ``_MIN_SECRET_VALUE_LEN`` are ignored.
    :returns: the string with every match replaced by :data:`REDACTED`.
    """
    known = sorted(
        {v for v in secret_values if v and len(v) >= _MIN_SECRET_VALUE_LEN},
        key=len,
        reverse=True,  # longest first, so a secret containing another masks whole
    )
    text = scrub_url_credentials(text)
    for value in known:
        text = text.replace(value, REDACTED)
    text = _TOKEN_RE.sub(REDACTED, text)
    text = _AUTH_HEADER_RE.sub(rf"\1 {REDACTED}", text)
    return _SECRET_ASSIGNMENT_RE.sub(rf"\1{REDACTED}", text)


def redact_sensitive(value: Any, secret_values: Optional[Iterable[str]] = None) -> Any:
    """Recursively copy a mapping/list, masking values under secret-looking keys.

    Strings (including those nested in dicts and lists) are also redacted in place,
    so a secret riding inside free text is masked even under a non-secret key: see
    :func:`_redact_string`. Pattern matching is best-effort — a secret with no
    recognizable shape under an innocuous name is only caught if it is passed in
    ``secret_values``.

    :param value: any value; dicts and lists are copied and recursed into, strings
        are redacted, other types are returned unchanged (no mutation of the input).
    :param secret_values: exact secret strings (e.g. a step's declared secrets) to
        mask wherever they appear in a string.
    :returns: a redacted deep-ish copy where any dict key matching
        ``SENSITIVE_KEY_RE`` has its value replaced with ``REDACTED`` and every
        surviving string has been through :func:`_redact_string`.
    """
    secret_values = list(secret_values or ())
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and SENSITIVE_KEY_RE.search(key)
                else redact_sensitive(val, secret_values)
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item, secret_values) for item in value]
    if isinstance(value, str):
        return _redact_string(value, secret_values)
    return value


# Cap on a log tail embedded in an event/PR body; the tail end (traceback) is kept.
LOG_TAIL_MAX_BYTES = 8192
_TRUNCATION_MARKER = "...[truncated]...\n"


def sanitize_log_tail(
    content: Optional[str], secret_values: Optional[Iterable[str]] = None
) -> Optional[str]:
    """Make a raw log tail safe to embed in an event/PR markdown code block.

    Shared by the LSF and SkyPilot failure paths. Redacts secrets
    (:func:`redact_sensitive`) on the full text first, so the cap below can't split a
    secret and leave a fragment the matchers no longer see; then caps the byte width
    at :data:`LOG_TAIL_MAX_BYTES` (keeping the tail end, where the traceback is, and
    prefixing a truncation marker); then neutralizes triple-backticks so log content
    can't close the ```` ``` ```` fence it is shown in. Display-only; nothing parses it.

    :param content: the raw tail, or ``None``.
    :param secret_values: exact secret strings to mask (see :func:`redact_sensitive`).
    :returns: the sanitized tail, or ``None`` for empty input.
    """
    if not content:
        return None
    content = redact_sensitive(content, secret_values)
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) > LOG_TAIL_MAX_BYTES:
        content = _TRUNCATION_MARKER + encoded[-LOG_TAIL_MAX_BYTES:].decode(
            "utf-8", errors="replace"
        )
    return content.replace("```", "``\u200b`")
