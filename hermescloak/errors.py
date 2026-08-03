"""Exceptions. Deliberately dependency-free so the .pth autoloader can import it early."""


class CloakFailClosed(Exception):
    """Masking failed while the profile asked for fail_mode: closed.

    Raised INSTEAD of quietly forwarding unmasked content. The caller is expected to
    let this propagate so the request never leaves the machine — a blocked request is
    the point of fail-closed. Everything else in HermesCloak fails open.
    """
