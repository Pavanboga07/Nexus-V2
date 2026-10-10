"""Typed provider-configuration failures (mapped to HTTP in api.errors)."""


class ProviderConfigError(Exception):
    """Base class for provider-configuration failures."""


class ProviderConfigInvalid(ProviderConfigError):
    """Provider, model, or key input failed validation."""


class CredentialStoreUnavailable(ProviderConfigError):
    """No credential master key is configured on this deployment."""


class CredentialNotConfigured(ProviderConfigError):
    """This owner has no saved provider configuration."""


class ProviderAuthError(ProviderConfigError):
    """The provider rejected the credentials."""


class ProviderUnavailableError(ProviderConfigError):
    """The provider could not be reached (network/timeout)."""


class ProviderCallError(ProviderConfigError):
    """The provider call failed without usable output."""
