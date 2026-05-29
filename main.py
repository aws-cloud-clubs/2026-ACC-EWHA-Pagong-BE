import os
import uuid
import shutil
from io import BytesIO
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional, List, Literal

import boto3  # pyright: ignore[reportMissingImports]
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from botocore.exceptions import BotoCoreError, ClientError  # pyright: ignore[reportMissingImports]


load_dotenv()


def get_env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def get_env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


APP_TITLE = os.getenv("APP_TITLE", "Project File Share API")
APP_DESCRIPTION = os.getenv("APP_DESCRIPTION", "프로젝트 산출물 공유 시스템 초기 API")
APP_VERSION = os.getenv("APP_VERSION", "0.1.0")
APP_ENV = os.getenv("APP_ENV", "development")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/file_share",
)
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
STORAGE_BACKEND_ENV = os.getenv("STORAGE_BACKEND")
STORAGE_BACKEND = (STORAGE_BACKEND_ENV or "").strip().lower()
S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
AWS_REGION = os.getenv("AWS_REGION", "").strip()
S3_REGION = os.getenv("S3_REGION", "").strip() or AWS_REGION or None
S3_PREFIX = os.getenv("S3_PREFIX", "uploads").strip().strip("/")

os.makedirs(UPLOAD_DIR, exist_ok=True)

if STORAGE_BACKEND not in {"", "local", "s3"}:
    raise RuntimeError("STORAGE_BACKEND는 local 또는 s3만 허용됩니다.")

# STORAGE_BACKEND 미지정 시 S3_BUCKET 존재 여부로 자동 선택합니다.
USE_S3 = STORAGE_BACKEND == "s3" or (STORAGE_BACKEND == "" and bool(S3_BUCKET))

if STORAGE_BACKEND == "s3" and not S3_BUCKET:
    raise RuntimeError("STORAGE_BACKEND=s3 인 경우 S3_BUCKET 설정이 필요합니다.")

s3_client = boto3.client("s3", region_name=S3_REGION) if USE_S3 else None

engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


def _build_s3_key(stored_filename: str) -> str:
    if not S3_PREFIX:
        return stored_filename
    return f"{S3_PREFIX}/{stored_filename}"


def _parse_s3_storage_path(storage_path: str) -> tuple[str, str]:
    if not storage_path.startswith("s3://"):
        raise HTTPException(status_code=500, detail="S3 storage path 형식이 올바르지 않습니다.")

    remainder = storage_path[len("s3://"):]
    parts = remainder.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=500, detail="S3 storage path 형식이 올바르지 않습니다.")
    return parts[0], parts[1]


def save_uploaded_file(file: UploadFile, stored_filename: str) -> tuple[str, int]:
    if USE_S3:
        if s3_client is None:
            raise HTTPException(status_code=500, detail="S3 클라이언트가 초기화되지 않았습니다.")
        key = _build_s3_key(stored_filename)
        try:
            content = file.file.read()
            s3_client.put_object(
                Bucket=S3_BUCKET,
                Key=key,
                Body=content,
                ContentType=file.content_type or "application/octet-stream",
            )
        except (BotoCoreError, ClientError):
            raise HTTPException(status_code=500, detail="S3 파일 저장에 실패했습니다.")

        return f"s3://{S3_BUCKET}/{key}", len(content)

    storage_path = os.path.join(UPLOAD_DIR, stored_filename)
    try:
        with open(storage_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception:
        raise HTTPException(status_code=500, detail="파일 저장에 실패했습니다.")

    file_size = os.path.getsize(storage_path)
    return storage_path, file_size


def ensure_file_exists(storage_path: str) -> None:
    if storage_path.startswith("s3://"):
        if s3_client is None:
            raise HTTPException(status_code=500, detail="S3 클라이언트가 초기화되지 않았습니다.")
        bucket, key = _parse_s3_storage_path(storage_path)
        try:
            s3_client.head_object(Bucket=bucket, Key=key)
        except ClientError:
            raise HTTPException(status_code=404, detail="저장된 파일을 찾을 수 없습니다.")
        except BotoCoreError:
            raise HTTPException(status_code=500, detail="S3 파일 조회에 실패했습니다.")
        return

    if not os.path.exists(storage_path):
        raise HTTPException(status_code=404, detail="저장된 파일을 찾을 수 없습니다.")


def build_download_response(file_record: "StoredFile"):
    if file_record.storage_path.startswith("s3://"):
        if s3_client is None:
            raise HTTPException(status_code=500, detail="S3 클라이언트가 초기화되지 않았습니다.")
        bucket, key = _parse_s3_storage_path(file_record.storage_path)
        try:
            obj = s3_client.get_object(Bucket=bucket, Key=key)
            body = obj["Body"].read()
        except ClientError:
            raise HTTPException(status_code=404, detail="저장된 파일을 찾을 수 없습니다.")
        except BotoCoreError:
            raise HTTPException(status_code=500, detail="S3 파일 다운로드에 실패했습니다.")

        filename = file_record.original_filename.replace('"', "")
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        return StreamingResponse(
            BytesIO(body),
            media_type=file_record.mime_type or "application/octet-stream",
            headers=headers,
        )

    return FileResponse(
        path=file_record.storage_path,
        filename=file_record.original_filename,
        media_type=file_record.mime_type
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    startup()
    yield


app = FastAPI(
    title=APP_TITLE,
    description=APP_DESCRIPTION,
    version=APP_VERSION,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-secret-change-this")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = get_env_int("JWT_EXPIRE_MINUTES", 60 * 8)
AUTO_SEED_DATA = get_env_bool("AUTO_SEED_DATA", APP_ENV != "production")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)
    role = Column(String, nullable=False)  # EMPLOYEE, MANAGER, EXECUTIVE

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True)
    client_name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    status = Column(String, default="ACTIVE")  # ACTIVE, CLOSED

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    creator = relationship("User")
    client = relationship("Client")


class ProjectMember(Base):
    __tablename__ = "project_members"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    project_role = Column(String, nullable=False)  # LEADER, MEMBER, VIEWER

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    project = relationship("Project")


class StoredFile(Base):
    __tablename__ = "files"

    id = Column(Integer, primary_key=True, index=True)

    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    uploader_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    original_filename = Column(String, nullable=False)
    stored_filename = Column(String, nullable=False)
    file_size = Column(Integer, nullable=False)
    mime_type = Column(String, nullable=True)

    file_type = Column(String, nullable=False)  # WORKING, REPORT, CLIENT_FINAL
    storage_path = Column(String, nullable=False)
    status = Column(String, default="ACTIVE")  # ACTIVE, DELETED

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    uploader = relationship("User")
    project = relationship("Project")


class ShareLink(Base):
    __tablename__ = "share_links"

    id = Column(Integer, primary_key=True, index=True)
    file_id = Column(Integer, ForeignKey("files.id"), nullable=False)
    token = Column(String, unique=True, nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=True)
    client_name = Column(String, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    assigned_staff_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    status = Column(String, default="ACTIVE")  # ACTIVE, EXPIRED, REVOKED

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    file = relationship("StoredFile", foreign_keys=[file_id])
    client = relationship("Client", foreign_keys=[client_id])
    creator = relationship("User", foreign_keys=[created_by])
    assigned_staff = relationship("User", foreign_keys=[assigned_staff_user_id])


class AuditLog(Base):
    __tablename__ = "access_logs"

    id = Column(Integer, primary_key=True, index=True)
    file_id = Column(Integer, ForeignKey("files.id"), nullable=False)
    share_link_id = Column(Integer, ForeignKey("share_links.id"), nullable=True)
    action = Column(String, nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    actor_type = Column(String, nullable=False)  # INTERNAL_USER, CUSTOMER
    ip_address = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    message = Column(String, nullable=True)
    result = Column(String, default="SUCCESS")  # SUCCESS, FAILED

    created_at = Column(DateTime, default=datetime.utcnow)

    file = relationship("StoredFile", foreign_keys=[file_id])
    share_link = relationship("ShareLink", foreign_keys=[share_link_id])
    actor_user = relationship("User", foreign_keys=[actor_user_id])


class LoginRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "employee@pagong.test",
                "password": "pagong1234",
            },
            "examples": [
                {
                    "email": "employee@pagong.test",
                    "password": "pagong1234",
                }
            ]
        }
    )

    email: str = Field(description="로그인 이메일", examples=["employee@pagong.test"])
    password: str = Field(description="로그인 비밀번호", examples=["pagong1234"])


class ProjectMemberInput(BaseModel):
    userId: int = Field(description="프로젝트에 추가할 사용자 ID", examples=[1])
    projectRole: Literal["MEMBER", "VIEWER"] = Field(
        description="프로젝트 내 권한. LEADER는 생성자가 자동 지정됩니다.",
        examples=["MEMBER"],
    )


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "A뷰티 여름 캠페인",
                "clientName": "A뷰티",
                "description": "여름 신제품 런칭 캠페인",
                "members": [
                    {
                        "userId": 1,
                        "projectRole": "MEMBER",
                    }
                ],
            },
            "examples": [
                {
                    "name": "A뷰티 여름 캠페인",
                    "clientName": "A뷰티",
                    "description": "여름 신제품 런칭 캠페인",
                    "members": [
                        {
                            "userId": 1,
                            "projectRole": "MEMBER",
                        }
                    ],
                }
            ]
        }
    )

    name: str = Field(description="프로젝트명", examples=["A뷰티 여름 캠페인"])
    clientName: str = Field(
        description="고객사명. 없으면 clients 테이블에 자동 생성됩니다.",
        examples=["A뷰티"],
    )
    description: Optional[str] = Field(
        default=None,
        description="프로젝트 설명",
        examples=["여름 신제품 런칭 캠페인"],
    )
    members: List[ProjectMemberInput] = Field(
        default_factory=list,
        description="프로젝트에 함께 추가할 멤버 목록",
    )


class ProjectStaffUpdateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "staffUserIds": [1],
            },
            "examples": [
                {
                    "staffUserIds": [1],
                }
            ]
        }
    )

    staffUserIds: List[int] = Field(
        description="프로젝트 담당 직원으로 지정할 EMPLOYEE 사용자 ID 목록",
        examples=[[1]],
    )


class ShareLinkCreateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "clientName": "A뷰티",
                "expiresInDays": 7,
            },
            "examples": [
                {
                    "clientName": "A뷰티",
                    "expiresInDays": 7,
                }
            ]
        }
    )

    clientName: str = Field(
        description="공유 링크를 전달할 고객사명. 없으면 clients 테이블에 자동 생성됩니다.",
        examples=["A뷰티"],
    )
    expiresInDays: Literal[1, 3, 7] = Field(
        description="공유 링크 만료 기간. 1, 3, 7일만 허용됩니다.",
        examples=[7],
    )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_access_token(user: User) -> str:
    now = datetime.utcnow()
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db)
):
    if not credentials:
        raise HTTPException(status_code=401, detail="Authorization header가 필요합니다.")

    if credentials.scheme != "Bearer":
        raise HTTPException(status_code=401, detail="Bearer token 형식이 아닙니다.")

    token = credentials.credentials

    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id = int(payload.get("sub", "0"))
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="토큰이 만료되었습니다.")
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="사용자를 찾을 수 없습니다.")

    return user


def is_project_member(db: Session, user_id: int, project_id: int) -> bool:
    member = db.query(ProjectMember).filter(
        ProjectMember.user_id == user_id,
        ProjectMember.project_id == project_id
    ).first()

    return member is not None


def get_project_member_role(db: Session, user_id: int, project_id: int) -> Optional[str]:
    member = db.query(ProjectMember).filter(
        ProjectMember.user_id == user_id,
        ProjectMember.project_id == project_id
    ).first()

    if not member:
        return None

    return member.project_role


def can_access_file(user_role: str, file_type: str) -> bool:
    if file_type == "WORKING":
        return user_role in ["EMPLOYEE", "MANAGER", "EXECUTIVE"]

    if file_type == "REPORT":
        return user_role in ["MANAGER", "EXECUTIVE"]

    if file_type == "CLIENT_FINAL":
        return user_role in ["MANAGER", "EXECUTIVE"]

    return False


def validate_file_type(file_type: str):
    allowed = ["WORKING", "REPORT", "CLIENT_FINAL"]

    if file_type not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"fileType은 {allowed} 중 하나여야 합니다."
        )


def validate_project_role(project_role: str):
    allowed = ["MEMBER", "VIEWER"]

    if project_role not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"projectRole은 {allowed} 중 하나여야 합니다."
        )


def get_or_create_client_by_name(db: Session, client_name: str) -> Client:
    normalized = client_name.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="고객사명은 필수입니다.")

    client = db.query(Client).filter(Client.name == normalized).first()
    if client:
        return client

    client = Client(name=normalized)
    db.add(client)
    try:
        db.commit()
        db.refresh(client)
        return client
    except IntegrityError:
        db.rollback()
        client = db.query(Client).filter(Client.name == normalized).first()
        if not client:
            raise HTTPException(status_code=500, detail="고객사 생성에 실패했습니다.")
        return client


def require_manager_or_executive(user: User):
    if user.role not in ["MANAGER", "EXECUTIVE"]:
        raise HTTPException(status_code=403, detail="팀장 또는 임원 권한이 필요합니다.")


def validate_share_link_expiry_days(expires_in_days: int):
    allowed = [1, 3, 7]
    if expires_in_days not in allowed:
        raise HTTPException(status_code=400, detail=f"expiresInDays는 {allowed} 중 하나여야 합니다.")


def is_project_staff_assignee(db: Session, project_id: int, user_id: int) -> bool:
    user = db.query(User).filter(User.id == user_id).first()
    if not user or user.role != "EMPLOYEE":
        return False

    member = db.query(ProjectMember).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id == user_id,
        ProjectMember.project_role == "MEMBER"
    ).first()
    return member is not None


def get_client_ip(request: Request) -> Optional[str]:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def write_audit_log(
    db: Session,
    file_id: int,
    action: str,
    request: Request,
    actor_user_id: Optional[int] = None,
    actor_type: str = "INTERNAL_USER",
    share_link_id: Optional[int] = None,
    message: Optional[str] = None,
    result: str = "SUCCESS",
):
    log = AuditLog(
        file_id=file_id,
        share_link_id=share_link_id,
        action=action,
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        message=message,
        result=result,
    )
    db.add(log)
    db.commit()


def get_active_share_link(db: Session, token: str) -> ShareLink:
    share_link = db.query(ShareLink).filter(ShareLink.token == token).first()
    if not share_link:
        raise HTTPException(status_code=404, detail="공유 링크를 찾을 수 없습니다.")

    if share_link.status == "REVOKED":
        raise HTTPException(status_code=410, detail="비활성화된 공유 링크입니다.")

    now = datetime.utcnow()
    if share_link.expires_at < now:
        if share_link.status != "EXPIRED":
            share_link.status = "EXPIRED"
            share_link.updated_at = now
            db.commit()
        raise HTTPException(status_code=410, detail="만료된 공유 링크입니다.")

    return share_link


def startup():
    if APP_ENV == "production" and JWT_SECRET_KEY == "dev-secret-change-this":
        raise RuntimeError("JWT_SECRET_KEY must be set in production.")

    if USE_S3:
        try:
            s3_client.head_bucket(Bucket=S3_BUCKET)
        except ClientError as exc:
            raise RuntimeError("S3 연결 확인에 실패했습니다. S3_BUCKET/권한/리전을 확인하세요.") from exc
        except BotoCoreError as exc:
            raise RuntimeError("S3 클라이언트 초기화에 실패했습니다.") from exc

    if not AUTO_SEED_DATA:
        return

    db = SessionLocal()

    try:
        try:
            user_count = db.query(User).count()
        except OperationalError as exc:
            raise RuntimeError(
                "Database schema is not initialized. Run 'alembic upgrade head' first."
            ) from exc

        if user_count > 0:
            return

        employee = User(
            id=1,
            name="사원 A",
            email="employee@brandwave.com",
            password="password1234",
            role="EMPLOYEE"
        )

        manager = User(
            id=2,
            name="팀장 B",
            email="manager@brandwave.com",
            password="password1234",
            role="MANAGER"
        )

        executive = User(
            id=3,
            name="임원 C",
            email="executive@brandwave.com",
            password="password1234",
            role="EXECUTIVE"
        )

        db.add_all([employee, manager, executive])
        db.commit()

        client1 = Client(id=1, name="A뷰티")
        client2 = Client(id=2, name="B식품")
        db.add_all([client1, client2])
        db.commit()

        project1 = Project(
            id=1,
            name="A뷰티 여름 캠페인",
            client_id=1,
            client_name="A뷰티",
            description="여름 신제품 런칭 캠페인",
            status="ACTIVE",
            created_by=2
        )

        project2 = Project(
            id=2,
            name="B식품 SNS 광고 프로젝트",
            client_id=2,
            client_name="B식품",
            description="SNS 광고 콘텐츠 제작 프로젝트",
            status="ACTIVE",
            created_by=2
        )

        db.add_all([project1, project2])
        db.commit()

        members = [
            ProjectMember(project_id=1, user_id=2, project_role="LEADER"),
            ProjectMember(project_id=1, user_id=1, project_role="MEMBER"),
            ProjectMember(project_id=1, user_id=3, project_role="VIEWER"),

            ProjectMember(project_id=2, user_id=2, project_role="LEADER"),
            ProjectMember(project_id=2, user_id=3, project_role="VIEWER"),
        ]

        db.add_all(members)
        db.commit()

    finally:
        db.close()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(
        User.email == payload.email,
        User.password == payload.password
    ).first()

    if not user:
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다.")

    token = create_access_token(user)

    return {
        "accessToken": token,
        "tokenType": "Bearer",
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role
        }
    }


@app.get("/api/auth/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "role": current_user.role
    }


@app.get("/api/clients")
def get_clients(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    clients = db.query(Client).order_by(Client.id.asc()).all()

    return {
        "clients": [
            {
                "clientId": client.id,
                "clientName": client.name
            }
            for client in clients
        ]
    }


@app.post("/api/projects", status_code=201)
def create_project(
    payload: ProjectCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    require_manager_or_executive(current_user)

    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="프로젝트명은 필수입니다.")

    client = get_or_create_client_by_name(db, payload.clientName)

    members = payload.members or []

    for member in members:
        validate_project_role(member.projectRole)

    member_user_ids = [member.userId for member in members]

    if member_user_ids:
        existing_users = db.query(User).filter(User.id.in_(member_user_ids)).all()
        existing_user_ids = {user.id for user in existing_users}

        invalid_user_ids = [
            user_id for user_id in member_user_ids
            if user_id not in existing_user_ids
        ]

        if invalid_user_ids:
            raise HTTPException(
                status_code=400,
                detail=f"존재하지 않는 사용자 ID가 포함되어 있습니다: {invalid_user_ids}"
            )

    project = Project(
        name=payload.name,
        client_id=client.id,
        client_name=client.name,
        description=payload.description,
        status="ACTIVE",
        created_by=current_user.id
    )

    db.add(project)
    db.commit()
    db.refresh(project)

    project_members = [
        ProjectMember(
            project_id=project.id,
            user_id=current_user.id,
            project_role="LEADER"
        )
    ]

    added_user_ids = {current_user.id}

    for member in members:
        if member.userId in added_user_ids:
            continue

        project_members.append(
            ProjectMember(
                project_id=project.id,
                user_id=member.userId,
                project_role=member.projectRole
            )
        )

        added_user_ids.add(member.userId)

    db.add_all(project_members)
    db.commit()

    saved_members = db.query(ProjectMember).filter(
        ProjectMember.project_id == project.id
    ).all()

    return {
        "projectId": project.id,
        "name": project.name,
        "clientId": project.client_id,
        "clientName": project.client_name,
        "description": project.description,
        "status": project.status,
        "createdBy": project.created_by,
        "members": [
            {
                "userId": member.user.id,
                "name": member.user.name,
                "email": member.user.email,
                "projectRole": member.project_role
            }
            for member in saved_members
        ],
        "createdAt": project.created_at
    }


@app.get("/api/projects")
def get_projects(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    memberships = db.query(ProjectMember).filter(
        ProjectMember.user_id == current_user.id
    ).all()

    return {
        "projects": [
            {
                "projectId": membership.project.id,
                "name": membership.project.name,
                "clientId": membership.project.client_id,
                "clientName": membership.project.client_name,
                "status": membership.project.status,
                "myProjectRole": membership.project_role,
                "createdAt": membership.project.created_at,
                "updatedAt": membership.project.updated_at
            }
            for membership in memberships
        ]
    }


@app.get("/api/projects/{project_id}")
def get_project_detail(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    project = db.query(Project).filter(Project.id == project_id).first()

    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    if not is_project_member(db, current_user.id, project_id):
        raise HTTPException(status_code=403, detail="프로젝트 접근 권한이 없습니다.")

    members = db.query(ProjectMember).filter(
        ProjectMember.project_id == project_id
    ).all()

    return {
        "projectId": project.id,
        "name": project.name,
        "clientId": project.client_id,
        "clientName": project.client_name,
        "description": project.description,
        "status": project.status,
        "createdBy": project.created_by,
        "members": [
            {
                "userId": member.user.id,
                "name": member.user.name,
                "email": member.user.email,
                "projectRole": member.project_role
            }
            for member in members
        ]
    }


@app.get("/api/projects/{project_id}/staff-assignees")
def get_project_staff_assignees(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    require_manager_or_executive(current_user)

    if not is_project_member(db, current_user.id, project_id):
        raise HTTPException(status_code=403, detail="프로젝트 접근 권한이 없습니다.")

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    assignee_members = db.query(ProjectMember).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.project_role == "MEMBER"
    ).all()

    return {
        "projectId": project_id,
        "staffAssignees": [
            {
                "userId": member.user.id,
                "name": member.user.name,
                "email": member.user.email
            }
            for member in assignee_members
            if member.user and member.user.role == "EMPLOYEE"
        ]
    }


@app.put("/api/projects/{project_id}/staff-assignees")
def update_project_staff_assignees(
    project_id: int,
    payload: ProjectStaffUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    require_manager_or_executive(current_user)

    if not is_project_member(db, current_user.id, project_id):
        raise HTTPException(status_code=403, detail="프로젝트 접근 권한이 없습니다.")

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    unique_user_ids = list(dict.fromkeys(payload.staffUserIds))
    if unique_user_ids:
        users = db.query(User).filter(User.id.in_(unique_user_ids)).all()
        user_map = {user.id: user for user in users}

        invalid_user_ids = [user_id for user_id in unique_user_ids if user_id not in user_map]
        if invalid_user_ids:
            raise HTTPException(
                status_code=400,
                detail=f"존재하지 않는 사용자 ID가 포함되어 있습니다: {invalid_user_ids}"
            )

        non_employee_ids = [
            user_id for user_id in unique_user_ids
            if user_map[user_id].role != "EMPLOYEE"
        ]
        if non_employee_ids:
            raise HTTPException(
                status_code=400,
                detail=f"담당 직원은 EMPLOYEE만 지정할 수 있습니다: {non_employee_ids}"
            )

        not_member_ids = [
            user_id for user_id in unique_user_ids
            if not is_project_member(db, user_id, project_id)
        ]
        if not_member_ids:
            raise HTTPException(
                status_code=400,
                detail=f"프로젝트 참여자가 아닌 사용자가 포함되어 있습니다: {not_member_ids}"
            )

    employee_members = db.query(ProjectMember).join(User, ProjectMember.user_id == User.id).filter(
        ProjectMember.project_id == project_id,
        User.role == "EMPLOYEE"
    ).all()

    for member in employee_members:
        member.project_role = "VIEWER"

    selected_set = set(unique_user_ids)
    selected_members = db.query(ProjectMember).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id.in_(selected_set)
    ).all()
    for member in selected_members:
        member.project_role = "MEMBER"

    db.commit()

    saved_members = db.query(ProjectMember).join(User, ProjectMember.user_id == User.id).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.project_role == "MEMBER",
        User.role == "EMPLOYEE"
    ).all()

    return {
        "projectId": project_id,
        "staffAssignees": [
            {
                "userId": member.user.id,
                "name": member.user.name,
                "email": member.user.email
            }
            for member in saved_members
        ]
    }


@app.post("/api/projects/{project_id}/files", status_code=201)
def upload_file(
    project_id: int,
    fileType: Literal["WORKING", "REPORT", "CLIENT_FINAL"] = Form(
        ...,
        description="파일 분류. WORKING, REPORT, CLIENT_FINAL 중 하나",
        examples=["WORKING"],
    ),
    file: UploadFile = File(..., description="업로드할 파일"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    validate_file_type(fileType)

    project = db.query(Project).filter(Project.id == project_id).first()

    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    if not is_project_member(db, current_user.id, project_id):
        raise HTTPException(status_code=403, detail="프로젝트 접근 권한이 없습니다.")

    ext = os.path.splitext(file.filename)[1]
    stored_filename = f"{uuid.uuid4()}{ext}"
    storage_path, file_size = save_uploaded_file(file, stored_filename)

    file_record = StoredFile(
        project_id=project_id,
        uploader_id=current_user.id,
        original_filename=file.filename,
        stored_filename=stored_filename,
        file_size=file_size,
        mime_type=file.content_type,
        file_type=fileType,
        storage_path=storage_path,
        status="ACTIVE"
    )

    db.add(file_record)
    db.commit()
    db.refresh(file_record)

    return {
        "fileId": file_record.id,
        "projectId": file_record.project_id,
        "originalFilename": file_record.original_filename,
        "fileSize": file_record.file_size,
        "mimeType": file_record.mime_type,
        "fileType": file_record.file_type,
        "uploadedBy": file_record.uploader_id,
        "uploadedAt": file_record.created_at
    }


@app.post("/api/files/{file_id}/share-links", status_code=201)
def create_share_link(
    file_id: int,
    payload: ShareLinkCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    require_manager_or_executive(current_user)
    validate_share_link_expiry_days(payload.expiresInDays)

    file_record = db.query(StoredFile).filter(
        StoredFile.id == file_id,
        StoredFile.status == "ACTIVE"
    ).first()
    if not file_record:
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")

    if not is_project_member(db, current_user.id, file_record.project_id):
        raise HTTPException(status_code=403, detail="프로젝트 접근 권한이 없습니다.")

    client = get_or_create_client_by_name(db, payload.clientName)

    project = file_record.project
    if project:
        if project.client_id is not None and project.client_id != client.id:
            raise HTTPException(
                status_code=400,
                detail="파일의 프로젝트 고객사와 clientName이 일치하지 않습니다.",
            )
        if project.client_id is None and project.client_name != client.name:
            raise HTTPException(
                status_code=400,
                detail="파일의 프로젝트 고객사와 clientName이 일치하지 않습니다.",
            )

    token = uuid.uuid4().hex
    expires_at = datetime.utcnow() + timedelta(days=payload.expiresInDays)
    share_link = ShareLink(
        file_id=file_id,
        token=token,
        client_id=client.id,
        client_name=client.name,
        created_by=current_user.id,
        expires_at=expires_at,
        status="ACTIVE"
    )

    db.add(share_link)
    db.commit()
    db.refresh(share_link)

    base_url = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    share_url = f"{base_url}/api/share-links/{share_link.token}"

    return {
        "shareLinkId": share_link.id,
        "fileId": share_link.file_id,
        "projectId": file_record.project_id,
        "token": share_link.token,
        "url": share_url,
        "clientId": share_link.client_id,
        "clientName": share_link.client_name,
        "createdBy": share_link.created_by,
        "expiresAt": share_link.expires_at,
        "status": share_link.status
    }


@app.get("/api/share-links/{token}")
def get_share_link(token: str, request: Request, db: Session = Depends(get_db)):
    share_link = get_active_share_link(db, token)
    file_record = db.query(StoredFile).filter(
        StoredFile.id == share_link.file_id,
        StoredFile.status == "ACTIVE"
    ).first()
    if not file_record:
        raise HTTPException(status_code=404, detail="공유 대상 파일을 찾을 수 없습니다.")

    write_audit_log(
        db=db,
        file_id=file_record.id,
        share_link_id=share_link.id,
        action="VIEW_SHARE_LINK",
        actor_type="CUSTOMER",
        request=request,
    )

    return {
        "shareLinkId": share_link.id,
        "fileId": file_record.id,
        "projectId": file_record.project_id,
        "originalFilename": file_record.original_filename,
        "fileSize": file_record.file_size,
        "mimeType": file_record.mime_type,
        "fileType": file_record.file_type,
        "clientId": share_link.client_id,
        "clientName": share_link.client_name,
        "createdBy": share_link.created_by,
        "expiresAt": share_link.expires_at,
        "status": share_link.status
    }


@app.get("/api/share-links/{token}/download")
def download_share_link_file(token: str, request: Request, db: Session = Depends(get_db)):
    share_link = get_active_share_link(db, token)
    file_record = db.query(StoredFile).filter(
        StoredFile.id == share_link.file_id,
        StoredFile.status == "ACTIVE"
    ).first()
    if not file_record:
        raise HTTPException(status_code=404, detail="공유 대상 파일을 찾을 수 없습니다.")

    ensure_file_exists(file_record.storage_path)

    write_audit_log(
        db=db,
        file_id=file_record.id,
        share_link_id=share_link.id,
        action="DOWNLOAD_SHARE_LINK",
        actor_type="CUSTOMER",
        request=request,
    )

    return build_download_response(file_record)


@app.get("/api/projects/{project_id}/files")
def get_project_files(
    project_id: int,
    fileType: Optional[Literal["WORKING", "REPORT", "CLIENT_FINAL"]] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    project = db.query(Project).filter(Project.id == project_id).first()

    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    if not is_project_member(db, current_user.id, project_id):
        raise HTTPException(status_code=403, detail="프로젝트 접근 권한이 없습니다.")

    query = db.query(StoredFile).filter(
        StoredFile.project_id == project_id,
        StoredFile.status == "ACTIVE"
    )

    if fileType:
        validate_file_type(fileType)
        query = query.filter(StoredFile.file_type == fileType)

    files = query.order_by(StoredFile.created_at.desc()).all()

    visible_files = [
        f for f in files
        if can_access_file(current_user.role, f.file_type)
    ]

    return {
        "files": [
            {
                "fileId": f.id,
                "originalFilename": f.original_filename,
                "fileType": f.file_type,
                "fileSize": f.file_size,
                "mimeType": f.mime_type,
                "uploaderName": f.uploader.name if f.uploader else None,
                "uploadedAt": f.created_at
            }
            for f in visible_files
        ],
        "totalCount": len(visible_files)
    }


@app.get("/api/files/{file_id}")
def get_file_detail(
    file_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    file_record = db.query(StoredFile).filter(
        StoredFile.id == file_id,
        StoredFile.status == "ACTIVE"
    ).first()

    if not file_record:
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")

    if not is_project_member(db, current_user.id, file_record.project_id):
        raise HTTPException(status_code=403, detail="프로젝트 접근 권한이 없습니다.")

    if not can_access_file(current_user.role, file_record.file_type):
        raise HTTPException(status_code=403, detail="파일 접근 권한이 없습니다.")

    write_audit_log(
        db=db,
        file_id=file_record.id,
        action="VIEW_FILE",
        actor_user_id=current_user.id,
        actor_type="INTERNAL_USER",
        request=request,
    )

    return {
        "fileId": file_record.id,
        "projectId": file_record.project_id,
        "originalFilename": file_record.original_filename,
        "storedFilename": file_record.stored_filename,
        "fileSize": file_record.file_size,
        "mimeType": file_record.mime_type,
        "fileType": file_record.file_type,
        "storagePath": file_record.storage_path,
        "uploaderName": file_record.uploader.name if file_record.uploader else None,
        "uploadedAt": file_record.created_at
    }


@app.get("/api/files/{file_id}/download")
def download_file(
    file_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    file_record = db.query(StoredFile).filter(
        StoredFile.id == file_id,
        StoredFile.status == "ACTIVE"
    ).first()

    if not file_record:
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")

    if not is_project_member(db, current_user.id, file_record.project_id):
        raise HTTPException(status_code=403, detail="프로젝트 접근 권한이 없습니다.")

    if not can_access_file(current_user.role, file_record.file_type):
        raise HTTPException(status_code=403, detail="파일 다운로드 권한이 없습니다.")

    ensure_file_exists(file_record.storage_path)

    write_audit_log(
        db=db,
        file_id=file_record.id,
        action="DOWNLOAD_FILE",
        actor_user_id=current_user.id,
        actor_type="INTERNAL_USER",
        request=request,
    )

    return build_download_response(file_record)


@app.get("/api/files/{file_id}/logs")
def get_file_access_logs(
    file_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    require_manager_or_executive(current_user)

    file_record = db.query(StoredFile).filter(StoredFile.id == file_id).first()
    if not file_record:
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")

    if not is_project_member(db, current_user.id, file_record.project_id):
        raise HTTPException(status_code=403, detail="프로젝트 접근 권한이 없습니다.")

    logs = db.query(AuditLog).filter(
        AuditLog.file_id == file_id
    ).order_by(AuditLog.created_at.desc()).all()

    return {
        "fileId": file_id,
        "logs": [
            {
                "logId": log.id,
                "action": log.action,
                "actorUserId": log.actor_user_id,
                "actorType": log.actor_type,
                "shareLinkId": log.share_link_id,
                "ipAddress": log.ip_address,
                "userAgent": log.user_agent,
                "message": log.message,
                "result": log.result,
                "createdAt": log.created_at
            }
            for log in logs
        ],
        "totalCount": len(logs)
    }
