"""Example Veloce application — demonstrating all MVP features."""

from pydantic import BaseModel

from veloce import (
    BackgroundTasks,
    CORSMiddleware,
    Depends,
    HTMLResponse,
    HTTPException,
    JSONResponse,
    Request,
    Router,
    Veloce,
)

# ── App setup ────────────────────────────────────────────────────
app = Veloce(title="Example API", version="1.0.0", debug=True)

# ── Middleware ───────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware(
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "DELETE"],
    )
)

# ── Lifecycle events ─────────────────────────────────────────────
db: dict = {}


@app.on_event("startup")
async def startup():
    db["users"] = {}
    print("Database initialized")


@app.on_event("shutdown")
async def shutdown():
    print("Shutting down...")


# ── Dependency injection ─────────────────────────────────────────
def get_db():
    return db


async def get_current_user(request: Request):
    token = request.headers.get("authorization", "")
    if not token:
        raise HTTPException(401, "Not authenticated")
    return {"id": 1, "name": "admin"}


# ── Pydantic models ─────────────────────────────────────────────
class UserCreate(BaseModel):
    name: str
    email: str
    age: int = 0


class UserResponse(BaseModel):
    id: int
    name: str
    email: str


# ── Basic routes ─────────────────────────────────────────────────
@app.get("/")
async def index(request: Request):
    return {"message": "Welcome to Veloce!", "version": app.version}


@app.get("/hello/{name}")
async def hello(name: str):
    return {"hello": name}


@app.get("/html")
async def html_page(request: Request):
    return HTMLResponse("<h1>Hello from Veloce!</h1>")


# ── Query parameters ────────────────────────────────────────────
@app.get("/search")
async def search(request: Request, q: str = "", page: int = 1, limit: int = 10):
    return {"query": q, "page": page, "limit": limit}


# ── Request body with Pydantic validation ────────────────────────
@app.post("/users")
async def create_user(user: UserCreate, database=Depends(get_db)):
    user_id = len(database["users"]) + 1
    database["users"][user_id] = user.model_dump()
    return {"id": user_id, **user.model_dump()}


# ── Path parameters with type coercion ───────────────────────────
@app.get("/users/{user_id}")
async def get_user(user_id: int, database=Depends(get_db)):
    if user_id not in database["users"]:
        raise HTTPException(404, f"User {user_id} not found")
    return {"id": user_id, **database["users"][user_id]}


# ── Protected route with dependency ──────────────────────────────
@app.get("/me", dependencies=[Depends(get_current_user)])
async def get_me(current_user=Depends(get_current_user)):
    return current_user


# ── Blueprint / Router grouping ──────────────────────────────────
api_v2 = Router(prefix="/api/v2", tags=["v2"])


@api_v2.get("/status")
async def api_status(request: Request):
    return {"status": "ok", "api_version": "v2"}


@api_v2.post("/echo")
async def echo(request: Request):
    body = await request.json()
    return {"echo": body}


app.include_router(api_v2)


# ── Custom exception handler ────────────────────────────────────
@app.exception_handler(HTTPException)
async def custom_http_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        {"error": exc.detail, "path": request.path},
        status_code=exc.status_code,
    )


# ── Background tasks ────────────────────────────────────────────
async def send_notification(user_id: int, message: str):
    # Simulate async work
    import asyncio

    await asyncio.sleep(0.01)
    print(f"Notification sent to user {user_id}: {message}")


@app.post("/notify/{user_id}")
async def notify(user_id: int, tasks: BackgroundTasks):
    tasks.add_task(send_notification, user_id, "Welcome!")
    return {"status": "notification queued"}


# ── Run ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
