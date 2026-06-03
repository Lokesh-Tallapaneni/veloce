---
description: Render Jinja2 templates in Veloce with Jinja2Templates, the render_template / render_template_string / stream_template helpers, template folders, filters, globals, and context processors.
tags: [templates, jinja2, html, rendering]
---

# Templates

Veloce renders HTML with [Jinja2](https://jinja.palletsprojects.com/). You can
use the class-based [`Jinja2Templates`](../reference.md#veloce.Jinja2Templates)
API explicitly, or the Flask-style [`render_template`](../reference.md#veloce.render_template)
shortcut once a template folder is configured on the app. Jinja2 is an optional
dependency — install it alongside Veloce.

```bash
pip install veloceframework jinja2
```

## A first template

Create a `templates/` directory next to your app module with a file
`hello.html`:

```html title="templates/hello.html"
<!doctype html>
<title>Hello</title>
<h1>Hello, {{ name }}</h1>
```

Construct a `Jinja2Templates` pointed at that directory and return a rendered
response from a handler:

```python title="app.py"
from veloce import Jinja2Templates, Request, Veloce

app = Veloce()
templates = Jinja2Templates(directory="templates")


@app.get("/hello/{name}")
async def hello(request: Request, name: str):
    return templates.TemplateResponse(
        "hello.html",
        {"request": request, "name": name},
    )
```

`TemplateResponse(name, context)` renders the named template against the
`context` dict and returns an [`HTMLResponse`](../reference.md#veloce.HTMLResponse).
The context is a plain dictionary — every variable the template uses must be a
key in it.

`name` may also be a list of candidate template names; the first one that
exists on disk is rendered, so you can fall back from a specific template to a
generic one. The same applies to `render_template`, `stream_template`, and
`get_template`:

```python
templates.TemplateResponse(["user_dashboard.html", "dashboard.html"], context)
```

`TemplateResponse` also accepts `media_type=...` to override the
`Content-Type` (it defaults to `text/html`) and `background=...` to attach a
[background task](background-tasks.md) — a callable, a `BackgroundTask`, or a
`BackgroundTasks` — that runs after the response is sent.

!!! note
    Pass the `request` object in the context (`{"request": request, ...}`) as
    shown above. This mirrors the convention used throughout Veloce's own
    examples and keeps the request available to templates and context
    processors. See [Requests and responses](requests-responses.md).

## Configuring the template folder on the app

The Flask-style helpers — [`render_template`](../reference.md#veloce.render_template),
[`render_template_string`](../reference.md#veloce.render_template_string),
and [`stream_template`](../reference.md#veloce.stream_template) — look up a
`Jinja2Templates` instance bound to the active application. The simplest way to
bind one is to pass `template_folder` to the `Veloce` constructor:

```python title="app.py"
from veloce import Request, Veloce, render_template

app = Veloce(template_folder="templates")


@app.get("/hello/{name}")
async def hello(request: Request, name: str):
    return render_template("hello.html", name=name)
```

When `template_folder` is relative, it is resolved against the app's package
directory (the folder containing the module named by `import_name`), so the
location does not depend on the current working directory.

`render_template(template_name, **context)` takes the context as keyword
arguments and returns the rendered string. Returning a string from a handler
produces an HTML response, so no explicit wrapping is needed for the common
case. If you need to set a status code or headers, wrap the result yourself:

```python title="app.py"
from veloce import HTMLResponse, Request, Veloce, render_template

app = Veloce(template_folder="templates")


@app.get("/missing")
async def missing(request: Request):
    body = render_template("404.html")
    return HTMLResponse(content=body, status_code=404)
```

!!! warning
    `render_template`, `render_template_string`, and `stream_template` require
    an active application context. Calling them outside a request handler (or
    without a bound `Jinja2Templates`) raises `RuntimeError`. Inside a handler
    you are always within an app context.

## Inline string templates

[`render_template_string`](../reference.md#veloce.render_template_string)
renders a template from a source string rather than a file. It is convenient
for short, one-off fragments:

```python title="app.py"
from veloce import Request, Veloce, render_template_string

app = Veloce()


@app.get("/greet/{name}")
async def greet(request: Request, name: str):
    return render_template_string("<p>Hi, {{ name }}</p>", name=name)
```

Unlike `render_template`, this helper falls back to a transient Jinja
environment when no template folder is configured, so it works even without
`template_folder=`. When a `Jinja2Templates` instance *is* bound, it honours the
app's registered filters, globals, and context processors.

## Autoescaping

By default `Jinja2Templates` autoescapes templates whose name ends in `.html`,
`.htm`, `.xhtml`, or `.xml`. Variables interpolated into those templates are
HTML-escaped, which protects against [cross-site scripting](https://owasp.org/www-community/attacks/xss/).
To render a value as raw HTML, mark it safe with [`Markup`](../reference.md#veloce.Markup):

```python title="app.py"
from veloce import Markup, Request, Veloce, render_template_string

app = Veloce()


@app.get("/raw")
async def raw(request: Request):
    snippet = Markup("<em>emphasised</em>")
    return render_template_string("{{ snippet }}", snippet=snippet)
```

!!! warning
    Only wrap content you trust in `Markup`. Marking user-supplied input as safe
    defeats autoescaping and reintroduces XSS risk.

Pass `autoescape=` to the constructor to override the default policy with a
boolean or a callable:

```python title="app.py"
from veloce import Jinja2Templates

templates = Jinja2Templates(directory="templates", autoescape=True)
```

## Built-in template globals

When a template is rendered inside a request, Veloce makes three names
available without adding them to the context:

- `url_for` — build a URL for a named route, e.g. `{{ url_for('hello', name='world') }}`.
- `g` — the per-request [application-context object](helpers.md).
- `current_app` — the active application.

```html title="templates/nav.html"
<a href="{{ url_for('hello', name='world') }}">Say hello</a>
```

The route name passed to `url_for` is the handler's function name unless you
override it. See [Routing](routing.md) for reverse URL generation.

## Filters, globals, and tests

Register custom Jinja filters, globals, and tests on the application. They
become available in every render that runs inside the app's request scope.

```python title="app.py"
from veloce import Request, Veloce, render_template_string

app = Veloce(template_folder="templates")


@app.template_filter("shout")
def shout(value: str) -> str:
    return value.upper() + "!"


@app.template_global("app_name")
def app_name() -> str:
    return "Veloce"


@app.get("/demo")
async def demo(request: Request):
    return render_template_string("{{ 'hi' | shout }} from {{ app_name() }}")
```

The name argument is optional and defaults to the function's own name. Each
decorator has an imperative counterpart — `app.add_template_filter(func, name)`,
`app.add_template_global(func, name)`, and `app.add_template_test(func, name)` —
for registering callables you did not define inline.

## Context processors

A context processor is a callable that runs before each render and returns a
dict merged into every template's context. Use it to inject values that should
appear in many templates without repeating them in every handler:

```python title="app.py"
from veloce import Request, Veloce, render_template_string

app = Veloce(template_folder="templates")


@app.context_processor
def inject_site():
    return {"site_name": "Veloce Docs"}


@app.get("/about")
async def about(request: Request):
    return render_template_string("Welcome to {{ site_name }}")
```

Processors run in registration order and their dicts are merged. The explicit
context you pass to a render call wins over a processor's value on a key
collision.

## Accessing the Jinja environment

When templating is configured, `app.jinja_env` exposes the underlying Jinja
`Environment` for advanced configuration, and `app.jinja_loader` returns its
loader (or `None` when no templating is set up):

```python title="app.py"
from veloce import Veloce

app = Veloce(template_folder="templates")
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True
```

`app.jinja_env` raises `RuntimeError` if no template folder has been configured.

## Streaming large templates

For a response whose body is large or expensive to build, render it
incrementally instead of buffering the whole string. [`stream_template`](../reference.md#veloce.stream_template)
returns an iterator of string chunks; wrap it in a
[`StreamingResponse`](../reference.md#veloce.StreamingResponse):

```python title="app.py"
from veloce import Request, StreamingResponse, Veloce, stream_template

app = Veloce(template_folder="templates")


@app.get("/report")
async def report(request: Request):
    rows = range(10000)
    return StreamingResponse(stream_template("report.html", rows=rows))
```

The chunks render lazily as the response body is consumed. Veloce captures the
request context so template globals such as `url_for` resolve correctly during
streaming, even though the body is emitted after the handler returns.

## Asynchronous rendering

`Jinja2Templates` also offers `render_async`, which uses a separate
async-enabled Jinja environment so templates performing async I/O during
`{% include %}` resolve without blocking the event loop:

```python title="app.py"
from veloce import HTMLResponse, Jinja2Templates, Request, Veloce

app = Veloce()
templates = Jinja2Templates(directory="templates")


@app.get("/async")
async def async_page(request: Request):
    html = await templates.render_async("hello.html", {"name": "async"})
    return HTMLResponse(content=html)
```

The async environment is built lazily on first use, so apps that never render
asynchronously pay nothing for it.

## Next steps

- Serve CSS, JavaScript, and images referenced by your templates — see
  [Static files](static-files.md).
- Build links inside templates with `url_for` — see [Routing](routing.md).
- Use `g`, `flash`, and `current_app` from templates and handlers — see
  [Flask-style helpers](helpers.md).
- Full signatures are in the [API reference](../reference.md).
