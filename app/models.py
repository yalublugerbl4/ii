import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tgid = Column(BigInteger, unique=True, nullable=False, index=True)
    balance = Column(Numeric(10, 2), default=0.0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Admin(Base):
    __tablename__ = "admins"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tgid = Column(BigInteger, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Template(Base):
    __tablename__ = "templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    badge = Column(String(50), nullable=True)
    is_new = Column(Boolean, default=False, nullable=False)
    is_popular = Column(Boolean, default=False, nullable=False)
    default_prompt = Column(Text, nullable=True)
    preview_image_url = Column(Text, nullable=True)
    examples = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    generations = relationship("Generation", back_populates="template")


class Generation(Base):
    __tablename__ = "generations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tgid = Column(BigInteger, nullable=False, index=True)
    template_id = Column(UUID(as_uuid=True), ForeignKey("templates.id"), nullable=True)
    mode = Column(String(50), nullable=False, default="image")
    model = Column(String(100), nullable=False)
    aspect_ratio = Column(String(20), nullable=True)
    resolution = Column(String(50), nullable=True)
    output_format = Column(String(10), nullable=True)
    prompt = Column(Text, nullable=False)
    status = Column(String(50), nullable=False, default="queued")
    kie_task_id = Column(String(100), nullable=True)
    result_url = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    template = relationship("Template", back_populates="generations")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tgid = Column(BigInteger, nullable=False, index=True)
    yookassa_payment_id = Column(String(100), unique=True, nullable=True, index=True)
    amount = Column(Numeric(10, 2), nullable=False)
    tokens = Column(Numeric(10, 2), nullable=False)
    status = Column(String(50), nullable=False, default="pending")
    plan_code = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

