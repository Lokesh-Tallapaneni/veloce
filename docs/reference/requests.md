---
description: Veloce API reference - requests.
---

# Requests

The request object and the parsed containers it exposes.

::: veloce.Request
::: veloce.URL
::: veloce.Headers
::: veloce.QueryParams
::: veloce.Cookies
::: veloce.State
::: veloce.Address
::: veloce.FormData
::: veloce.UploadFile
::: veloce.AcceptHeader
::: veloce.Authorization
::: veloce.RangeSpec

The remaining header helpers are reached through the `veloce.http` gateway:
they parse or build a header value rather than describing a `Request`
attribute.

::: veloce.http.CacheControl
::: veloce.http.HeaderSet
::: veloce.http.parse_multipart_form
::: veloce.http.header_key
::: veloce.http.header_get
::: veloce.http.header_present
