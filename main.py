import os
import uuid
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional, List

import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship


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

os.makedirs(UPLOAD_DIR, exist_ok=True)

engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()


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


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    client_name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    status = Column(String, default="ACTIVE")  # ACTIVE, CLOSED

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    creator = relationship("User")


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


class LoginRequest(BaseModel):
    email: str
    password: str


class ProjectMemberInput(BaseModel):
    userId: int
    projectRole: str  # MEMBER, VIEWER


class ProjectCreateRequest(BaseModel):
    name: str
    clientName: str
    description: Optional[str] = None
    members: Optional[List[ProjectMemberInput]] = []


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


def require_manager_or_executive(user: User):
    if user.role not in ["MANAGER", "EXECUTIVE"]:
        raise HTTPException(status_code=403, detail="팀장 또는 임원 권한이 필요합니다.")


def startup():
    if APP_ENV == "production" and JWT_SECRET_KEY == "dev-secret-change-this":
        raise RuntimeError("JWT_SECRET_KEY must be set in production.")

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

        project1 = Project(
            id=1,
            name="A뷰티 여름 캠페인",
            client_name="A뷰티",
            description="여름 신제품 런칭 캠페인",
            status="ACTIVE",
            created_by=2
        )

        project2 = Project(
            id=2,
            name="B식품 SNS 광고 프로젝트",
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


@app.post("/api/projects", status_code=201)
def create_project(
    payload: ProjectCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    require_manager_or_executive(current_user)

    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="프로젝트명은 필수입니다.")

    if not payload.clientName.strip():
        raise HTTPException(status_code=400, detail="고객사명은 필수입니다.")

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
        client_name=payload.clientName,
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
                "clientName": membership.project.client_name,
                "status": membership.project.status,
                "myProjectRole": membership.project_role
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


@app.post("/api/projects/{project_id}/files", status_code=201)
def upload_file(
    project_id: int,
    fileType: str = Form(...),
    file: UploadFile = File(...),
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
    storage_path = os.path.join(UPLOAD_DIR, stored_filename)

    try:
        with open(storage_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception:
        raise HTTPException(status_code=500, detail="파일 저장에 실패했습니다.")

    file_size = os.path.getsize(storage_path)

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


@app.get("/api/projects/{project_id}/files")
def get_project_files(
    project_id: int,
    fileType: Optional[str] = None,
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

    if not os.path.exists(file_record.storage_path):
        raise HTTPException(status_code=404, detail="저장된 파일을 찾을 수 없습니다.")

    return FileResponse(
        path=file_record.storage_path,
        filename=file_record.original_filename,
        media_type=file_record.mime_type
    )