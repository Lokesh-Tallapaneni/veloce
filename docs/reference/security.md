---
description: Veloce API reference - security.
---

# Security

Authentication schemes, token handling, password hashing, and the signing primitives underneath them.

::: veloce.SecurityScheme
::: veloce.APIKeyHeader
::: veloce.APIKeyQuery
::: veloce.APIKeyCookie
::: veloce.HTTPBasic
::: veloce.HTTPBasicCredentials
::: veloce.HTTPBearer
::: veloce.HTTPDigest
::: veloce.HTTPDigestCredentials
::: veloce.OAuth2PasswordBearer
::: veloce.OAuth2PasswordRequestForm
::: veloce.OAuth2PasswordRequestFormStrict
::: veloce.OAuth2AuthorizationCodeBearer
::: veloce.OpenIdConnect
::: veloce.SessionAuth
::: veloce.login_session
::: veloce.logout_session
::: veloce.encode_jwt
::: veloce.decode_jwt
::: veloce.Claims
::: veloce.JWTError
::: veloce.InvalidTokenError
::: veloce.InvalidSignatureError
::: veloce.ExpiredSignatureError
::: veloce.ImmatureSignatureError
::: veloce.InvalidAudienceError
::: veloce.InvalidIssuerError
::: veloce.MissingClaimError
::: veloce.UnsupportedAlgorithmError
::: veloce.hash_password
::: veloce.hash_password_async
::: veloce.verify_password
::: veloce.verify_password_async
::: veloce.verify_and_needs_update
::: veloce.verify_and_needs_update_async
::: veloce.needs_rehash
::: veloce.is_strong_password
::: veloce.make_reset_token
::: veloce.check_reset_token
::: veloce.BadResetToken
::: veloce.Principal
::: veloce.current_principal
::: veloce.set_principal
::: veloce.Secret
::: veloce.Signer
::: veloce.BadSignature
::: veloce.BadTimeSignature
::: veloce.BadData
::: veloce.constant_time_compare
::: veloce.safe_join
::: veloce.secure_filename

## Audit

The structured form of `Veloce.security_audit()`. `veloce.audit.run(app)`
returns `Finding` objects; startup refuses to serve on an `error`.

::: veloce.Finding
::: veloce.AuditContext
::: veloce.AuditFailed
