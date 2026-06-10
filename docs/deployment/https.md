---
description: Serve Veloce over HTTPS — TLS termination at a reverse proxy, an in-process ssl_context for app.run, obtaining certificates, a dev-only self-signed flow, and HTTP-to-HTTPS redirects with ProxyFix.
tags: [deployment, https, tls, certificates]
---

# HTTPS

Production traffic should reach your app over TLS. There are two places to
terminate it: at a reverse proxy in front of Veloce (the production default),
or in-process via the [`ssl_context`](../reference.md#veloce.Veloce.run)
argument to `app.run()` (development and single-binary deploys). This page
covers both, how to obtain certificates, a self-signed flow for local testing,
and how to redirect plain HTTP to HTTPS.

!!! warning "TLS is not optional"
    Without TLS, credentials, cookies, and tokens travel in plaintext over the
    network. Every public deployment must serve HTTPS. The choice on this page
    is only *where* TLS is terminated, never *whether* to use it.

## Terminating TLS at a reverse proxy

The production-grade option is to let a reverse proxy (nginx, Caddy, Traefik,
or a cloud load balancer) terminate TLS, then forward plain HTTP to Veloce on
the loopback interface. The proxy owns the certificate and the TLS handshake;
Veloce never sees the encryption.

```nginx
server {
    listen 443 ssl;
    server_name example.com;

    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Behind the proxy, run Veloce under a hardened ASGI server such as uvicorn (see
[Run a server manually](manually.md)):

```bash
pip install uvicorn
uvicorn app:app --host 127.0.0.1 --port 8000
```

Because TLS terminates at the proxy, Veloce receives plain HTTP. To make
`request.scheme` report `https` (so generated URLs and redirects use the right
scheme), trust the proxy's forwarding headers with
[`ProxyFix`](../reference.md#veloce.ProxyFix).

```python title="app.py" hl_lines="4"
from veloce import ProxyFix, Request, Veloce

app = Veloce()
app.add_middleware(ProxyFix(x_proto=1, x_for=1, x_host=1))


@app.get("/whoami")
async def whoami(request: Request):
    return {"scheme": request.scheme, "host": request.host}
```

`x_proto=1` trusts one hop of `X-Forwarded-Proto`, so `request.scheme` becomes
`https` when the proxy sets it.

!!! warning "Only trust proxies you control"
    `ProxyFix` makes Veloce believe the forwarding headers. If a request can
    reach the app *without* passing through your proxy, a client can forge
    `X-Forwarded-Proto: https` and spoof the scheme. Bind the app to loopback
    (`127.0.0.1`) or a private network so the proxy is the only path in, and set
    each `x_*` count to the exact number of trusted hops. See
    [ProxyFix](../guide/middleware.md) for the full trust model.

## Terminating TLS in-process with `ssl_context`

The built-in `app.run()` server accepts an [`ssl.SSLContext`](https://docs.python.org/3/library/ssl.html#ssl.SSLContext).
When set, Veloce serves HTTPS directly with no proxy in front. This is useful
for local development over `https://`, intranet tools, and single-binary
deploys where there is no separate proxy process.

```python title="app.py" hl_lines="14 15 16"
import ssl

from veloce import Veloce

app = Veloce()


@app.get("/")
async def index():
    return {"secure": True}


if __name__ == "__main__":
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("cert.pem", "key.pem")
    app.run(port=8443, ssl_context=context)
```

The context is handed straight to the event loop's
`create_server(ssl=...)`; left `None` (the default) the serving path is
byte-for-byte the plain-HTTP path, so TLS cost is paid only when a context is
set.

!!! warning "`app.run()` is the development server"
    Veloce's built-in server is for local development and single-process
    deploys. For multi-worker production, terminate TLS at a reverse proxy and
    run under uvicorn or the gunicorn `VeloceWorker` instead — see
    [Server workers](workers.md). `app.run()` logs this reminder on startup.

## Obtaining certificates

A browser-trusted certificate comes from a Certificate Authority. For public
sites, the standard free path is [Let's Encrypt](https://letsencrypt.org/) via
the [certbot](https://certbot.eff.org/) ACME client, which issues and renews
90-day certificates automatically.

| Scenario | Certificate source |
| --- | --- |
| Public site behind a proxy | Let's Encrypt / certbot, managed by the proxy. |
| Caddy / Traefik as the proxy | Built-in automatic ACME — no manual step. |
| Internal / corporate host | Your organisation's internal CA. |
| Local development only | A self-signed certificate (next section). |

When a reverse proxy terminates TLS, the certificate lives with the proxy and
Veloce needs no certificate files at all. You only load a cert into Veloce when
you use `ssl_context` for in-process TLS.

## Self-signed certificates for development

To test the HTTPS path locally without a CA, generate a self-signed
certificate with OpenSSL:

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout key.pem -out cert.pem -days 365 \
    -subj "/CN=localhost"
```

Point `ssl_context` at the generated files and run on an HTTPS port:

```python title="app.py"
import ssl

from veloce import Veloce

app = Veloce()


@app.get("/")
async def index():
    return {"hello": "https"}


if __name__ == "__main__":
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("cert.pem", "key.pem")
    app.run(port=8443, ssl_context=context)
```

The app now answers at `https://localhost:8443`.

!!! warning "Self-signed certificates are dev-only"
    Browsers and HTTP clients reject a self-signed certificate with a trust
    error because no CA vouches for it. Use one only on `localhost` for local
    testing. Never ship a self-signed certificate to a public deployment — get
    a real one from a CA.

## Redirecting HTTP to HTTPS

Add [`HTTPSRedirectMiddleware`](../reference.md#veloce.HTTPSRedirectMiddleware)
to send plain HTTP requests to the `https://` URL with a 308 Permanent Redirect
(so non-GET methods keep their method and body).

It resolves the scheme from the ASGI scope first, then `X-Forwarded-Proto`.
Behind a proxy, install [`ProxyFix`](../reference.md#veloce.ProxyFix) first so
the forwarded scheme is trusted.

```python title="app.py"
from veloce import HTTPSRedirectMiddleware, ProxyFix, Veloce

app = Veloce()

# ProxyFix runs first so the forwarded scheme is trusted before the redirect.
app.add_middleware(ProxyFix(x_proto=1))
app.add_middleware(HTTPSRedirectMiddleware())


@app.get("/")
async def index():
    return {"ok": True}
```

The `/.well-known/acme-challenge/` prefix is exempt by default so Let's Encrypt
HTTP-01 validation still reaches the app over plain HTTP.

`exempt_paths=("/health/", ...)`
:   Serve more prefixes without redirecting.

`exempt_acme_challenge=False`
:   Drop the ACME default.

!!! note
    Many reverse proxies and load balancers can redirect HTTP to HTTPS at the
    edge before the request reaches Veloce. When the proxy already does this,
    the in-app redirect is redundant — prefer the edge redirect and keep the
    app handling only HTTPS traffic.

## Testing the redirect

The in-memory [`TestClient`](../reference.md#veloce.TestClient) drives the
middleware without a socket. Set `X-Forwarded-Proto: http` to simulate an
insecure request arriving through the trusted proxy.

```python
from veloce import HTTPSRedirectMiddleware, ProxyFix, TestClient, Veloce

app = Veloce()
app.add_middleware(ProxyFix(x_proto=1))
app.add_middleware(HTTPSRedirectMiddleware())


@app.get("/")
async def index():
    return {"ok": True}


client = TestClient(app)

resp = client.get("/", headers={"X-Forwarded-Proto": "http"}, follow_redirects=False)
assert resp.status_code == 308
assert resp.headers["location"].startswith("https://")
```

## Next steps

- Serve the app under uvicorn or the native server — see [Run a server manually](manually.md).
- Run multiple worker processes behind the proxy — see [Server workers](workers.md).
- Containerise the app and proxy together — see [Docker](docker.md).
- Full signatures are in the [API reference](../reference.md).
