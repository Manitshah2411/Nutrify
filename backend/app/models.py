from datetime import UTC, date, datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria
from werkzeug.security import check_password_hash as check_legacy_password_hash

from .extensions import bcrypt

db = SQLAlchemy()


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class SoftDeleteMixin:
    is_deleted = db.Column(db.Boolean, default=False, nullable=False, index=True)
    deleted_at = db.Column(db.DateTime, nullable=True)

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = utcnow()

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None


class User(db.Model, UserMixin, SoftDeleteMixin, TimestampMixin):
    __tablename__ = 'users'

    ROLE_MASTER_ADMIN = 'master_admin'
    ROLE_SCHOOL_ADMIN = 'school_admin'
    ROLE_USER = 'user'

    LEGACY_ROLE_ALIASES = {
        'admin': ROLE_MASTER_ADMIN,
        'school': ROLE_SCHOOL_ADMIN,
        'student': ROLE_USER,
    }
    _BCRYPT_PREFIXES = ('$2a$', '$2b$', '$2y$')
    _LEGACY_HASH_PREFIXES = ('pbkdf2:', 'scrypt:')

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), nullable=True, index=True)
    full_name = db.Column(db.String(120), nullable=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    primary_student_id = db.Column(
        db.Integer,
        db.ForeignKey('student_details.id', use_alter=True, name='fk_users_primary_student_id'),
        nullable=True,
        index=True,
    )
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, index=True)
    school_name = db.Column(db.String(120), nullable=True, index=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    last_password_change_at = db.Column(db.DateTime, nullable=True)
    session_version = db.Column(db.Integer, default=1, nullable=False)
    can_manage_students = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_meals = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_attendance = db.Column(db.Boolean, default=False, nullable=False)
    can_view_reports = db.Column(db.Boolean, default=False, nullable=False)
    can_manage_staff = db.Column(db.Boolean, default=False, nullable=False)
    can_approve_workflows = db.Column(db.Boolean, default=False, nullable=False)

    student_detail = db.relationship(
        'StudentDetail',
        backref='user',
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys='StudentDetail.user_id',
    )
    students = db.relationship(
        'StudentDetail',
        backref='school',
        lazy='dynamic',
        cascade="all, delete-orphan",
        foreign_keys='StudentDetail.school_id',
    )
    meal_plans = db.relationship(
        'MealPlan',
        backref='school_user',
        lazy='dynamic',
        cascade="all, delete-orphan",
        foreign_keys='MealPlan.school_id',
    )
    school_account = db.relationship(
        'User',
        remote_side=[id],
        foreign_keys=[school_id],
        backref=db.backref('staff_members', lazy='dynamic'),
    )
    managed_student = db.relationship(
        'StudentDetail',
        foreign_keys=[primary_student_id],
        post_update=True,
        backref=db.backref('linked_users', lazy='dynamic'),
    )
    recorded_attendance = db.relationship('Attendance', foreign_keys='Attendance.recorded_by_user_id', lazy='dynamic')
    password_reset_tokens = db.relationship(
        'PasswordResetToken',
        backref='user',
        lazy='dynamic',
        cascade="all, delete-orphan",
    )
    notifications = db.relationship(
        'Notification',
        backref='user',
        lazy='dynamic',
        cascade="all, delete-orphan",
        foreign_keys='Notification.user_id',
    )

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
        self.last_password_change_at = utcnow()
        self.session_version = (self.session_version or 0) + 1

    @property
    def uses_legacy_password_hash(self):
        password_hash = self.password_hash or ''
        return password_hash.startswith(self._LEGACY_HASH_PREFIXES)

    def check_password(self, password):
        password_hash = self.password_hash or ''
        if not password_hash or password is None:
            return False

        if password_hash.startswith(self._BCRYPT_PREFIXES):
            try:
                return bcrypt.check_password_hash(password_hash, password)
            except ValueError:
                return False

        if self.uses_legacy_password_hash:
            try:
                return check_legacy_password_hash(password_hash, password)
            except (TypeError, ValueError):
                return False

        try:
            return bcrypt.check_password_hash(password_hash, password)
        except ValueError:
            try:
                return check_legacy_password_hash(password_hash, password)
            except (TypeError, ValueError):
                return False

    @property
    def normalized_role(self):
        role = (self.role or '').strip().lower()
        return self.LEGACY_ROLE_ALIASES.get(role, role)

    def has_role(self, *roles):
        normalized_roles = {
            self.LEGACY_ROLE_ALIASES.get((role or '').strip().lower(), (role or '').strip().lower())
            for role in roles
        }
        return self.normalized_role in normalized_roles

    @property
    def display_name(self):
        return self.full_name or self.school_name or self.username

    @property
    def is_school_root(self):
        return self.has_role(self.ROLE_SCHOOL_ADMIN) and self.school_id is None

    @property
    def school_scope_id(self):
        if self.has_role(self.ROLE_MASTER_ADMIN):
            return self.school_id or self.id
        if self.has_role(self.ROLE_SCHOOL_ADMIN):
            return self.school_id or self.id
        if self.student_detail is not None:
            return self.student_detail.school_id
        if self.managed_student is not None:
            return self.managed_student.school_id
        return self.school_id

    @property
    def portal_student_detail(self):
        return self.student_detail or self.managed_student

    @property
    def can_manage_students_effective(self):
        return self.has_role(self.ROLE_MASTER_ADMIN) or self.is_school_root or (
            self.has_role(self.ROLE_SCHOOL_ADMIN) and self.can_manage_students
        )

    @property
    def can_manage_meals_effective(self):
        return self.has_role(self.ROLE_MASTER_ADMIN) or self.is_school_root or (
            self.has_role(self.ROLE_SCHOOL_ADMIN) and self.can_manage_meals
        )

    @property
    def can_manage_attendance_effective(self):
        return self.has_role(self.ROLE_MASTER_ADMIN) or self.is_school_root or (
            self.has_role(self.ROLE_SCHOOL_ADMIN) and self.can_manage_attendance
        )

    @property
    def can_view_reports_effective(self):
        return self.has_role(self.ROLE_MASTER_ADMIN) or self.is_school_root or (
            self.has_role(self.ROLE_SCHOOL_ADMIN) and self.can_view_reports
        )

    @property
    def can_manage_staff_effective(self):
        return self.has_role(self.ROLE_MASTER_ADMIN) or self.is_school_root or (
            self.has_role(self.ROLE_SCHOOL_ADMIN) and self.can_manage_staff
        )

    @property
    def can_approve_workflows_effective(self):
        return self.has_role(self.ROLE_MASTER_ADMIN) or self.is_school_root or (
            self.has_role(self.ROLE_SCHOOL_ADMIN) and self.can_approve_workflows
        )


class StudentDetail(db.Model, SoftDeleteMixin, TimestampMixin):
    __tablename__ = 'student_details'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False, index=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    full_name = db.Column(db.String(120), nullable=False)
    roll_no = db.Column(db.Integer, nullable=False, index=True)
    dob = db.Column(db.Date, nullable=False)
    sex = db.Column(db.String(10), nullable=False)
    grade = db.Column(db.Integer, nullable=False, index=True)
    section = db.Column(db.String(10), nullable=False)
    activity_level = db.Column(db.String(50), default='moderately_active')
    allergies = db.Column(db.Text, nullable=True)
    guardian_name = db.Column(db.String(120), nullable=True)
    guardian_email = db.Column(db.String(255), nullable=True)
    guardian_phone = db.Column(db.String(32), nullable=True)
    status = db.Column(db.String(32), default='active', nullable=False, index=True)

    health_metrics = db.relationship('HealthMetric', backref='student_detail', lazy='dynamic', cascade="all, delete-orphan")
    attendance_records = db.relationship('Attendance', backref='student_detail', lazy='dynamic', cascade="all, delete-orphan")
    feedback_entries = db.relationship('UserFeedback', backref='student_detail', lazy='dynamic', cascade="all, delete-orphan")

    @property
    def age(self):
        today = date.today()
        return today.year - self.dob.year - ((today.month, today.day) < (self.dob.month, self.dob.day))

    @property
    def latest_health_metric(self):
        return self.health_metrics.order_by(HealthMetric.record_date.desc()).first()

    @property
    def latest_height(self):
        metric = self.latest_health_metric
        return metric.height_cm if metric else None

    @property
    def latest_weight(self):
        metric = self.latest_health_metric
        return metric.weight_kg if metric else None

    @property
    def bmi(self):
        height = self.latest_height
        weight = self.latest_weight
        if height and weight and height > 0:
            height_in_meters = height / 100
            return weight / (height_in_meters ** 2)
        return None


class HealthMetric(db.Model, TimestampMixin):
    __tablename__ = 'health_metrics'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student_details.id'), nullable=False, index=True)
    record_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    height_cm = db.Column(db.Float, nullable=False)
    weight_kg = db.Column(db.Float, nullable=False)

    __table_args__ = (
        db.Index('ix_health_metrics_student_date', 'student_id', 'record_date'),
    )


class Attendance(db.Model, TimestampMixin):
    __tablename__ = 'attendance'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student_details.id'), nullable=False, index=True)
    attendance_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    recorded_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    approval_status = db.Column(db.String(32), default='approved', nullable=False, index=True)
    ate_breakfast = db.Column(db.Boolean, default=False, nullable=False)
    ate_lunch = db.Column(db.Boolean, default=False, nullable=False)
    ate_dinner = db.Column(db.Boolean, default=False, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('student_id', 'attendance_date', name='uq_attendance_student_date'),
        db.Index('ix_attendance_student_date', 'student_id', 'attendance_date'),
    )

    @property
    def was_present(self):
        return self.ate_breakfast or self.ate_lunch or self.ate_dinner


class Food(db.Model, TimestampMixin):
    __tablename__ = 'food'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    name = db.Column(db.String(100), nullable=False, unique=True, index=True)
    calories = db.Column(db.Float, nullable=False)
    protein = db.Column(db.Float, nullable=False)
    carbs = db.Column(db.Float, nullable=False)
    fats = db.Column(db.Float, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)

    school_owner = db.relationship('User', foreign_keys=[school_id], backref=db.backref('custom_foods', lazy='dynamic'))
    created_by_user = db.relationship('User', foreign_keys=[created_by_user_id], backref=db.backref('created_foods', lazy='dynamic'))


class MealPlan(db.Model, SoftDeleteMixin, TimestampMixin):
    __tablename__ = 'meal_plan'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    plan_date = db.Column(db.Date, nullable=False, index=True)
    title = db.Column(db.String(120), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(32), default='approved', nullable=False, index=True)
    recurrence_label = db.Column(db.String(64), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    approved_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    template_id = db.Column(db.Integer, db.ForeignKey('meal_templates.id'), nullable=True, index=True)

    items = db.relationship('MealPlanItem', backref='plan', lazy='selectin', cascade="all, delete-orphan")
    creator = db.relationship('User', foreign_keys=[created_by_user_id], backref=db.backref('drafted_meal_plans', lazy='dynamic'))
    approver = db.relationship('User', foreign_keys=[approved_by_user_id], backref=db.backref('workflow_approved_meal_plans', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('school_id', 'plan_date', name='uq_meal_plan_school_date'),
        db.Index('ix_meal_plan_school_date', 'school_id', 'plan_date'),
    )

    @property
    def is_approved(self):
        return self.status == 'approved'


class MealPlanItem(db.Model, TimestampMixin):
    __tablename__ = 'meal_plan_item'

    id = db.Column(db.Integer, primary_key=True)
    meal_plan_id = db.Column(db.Integer, db.ForeignKey('meal_plan.id'), nullable=False, index=True)
    food_id = db.Column(db.Integer, db.ForeignKey('food.id'), nullable=False, index=True)
    meal_type = db.Column(db.String(20), nullable=False, index=True)
    food = db.relationship('Food')

    __table_args__ = (
        db.Index('ix_meal_plan_item_plan_food', 'meal_plan_id', 'food_id'),
    )


class MealTemplate(db.Model, SoftDeleteMixin, TimestampMixin):
    __tablename__ = 'meal_templates'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)

    school = db.relationship('User', foreign_keys=[school_id], backref=db.backref('meal_templates', lazy='dynamic'))
    creator = db.relationship('User', foreign_keys=[created_by_user_id], backref=db.backref('created_templates', lazy='dynamic'))
    items = db.relationship('MealTemplateItem', backref='template', lazy='selectin', cascade="all, delete-orphan")

    __table_args__ = (
        db.UniqueConstraint('school_id', 'name', name='uq_meal_template_school_name'),
    )


class MealTemplateItem(db.Model, TimestampMixin):
    __tablename__ = 'meal_template_items'

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('meal_templates.id'), nullable=False, index=True)
    food_id = db.Column(db.Integer, db.ForeignKey('food.id'), nullable=False, index=True)
    meal_type = db.Column(db.String(20), nullable=False, index=True)

    food = db.relationship('Food')


class AuditLog(db.Model, TimestampMixin):
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    action = db.Column(db.String(80), nullable=False, index=True)
    entity_type = db.Column(db.String(80), nullable=False, index=True)
    entity_id = db.Column(db.String(80), nullable=True, index=True)
    status = db.Column(db.String(32), default='success', nullable=False, index=True)
    ip_address = db.Column(db.String(64), nullable=True)
    details = db.Column(db.JSON, nullable=True)

    school = db.relationship('User', foreign_keys=[school_id], backref=db.backref('school_audit_logs', lazy='dynamic'))
    actor = db.relationship('User', foreign_keys=[actor_user_id], backref=db.backref('actor_audit_logs', lazy='dynamic'))


class PasswordResetToken(db.Model, TimestampMixin):
    __tablename__ = 'password_reset_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    token_hash = db.Column(db.String(128), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    consumed_at = db.Column(db.DateTime, nullable=True, index=True)
    requested_ip = db.Column(db.String(64), nullable=True)

    @property
    def is_active(self):
        return self.consumed_at is None and self.expires_at >= utcnow()


class Notification(db.Model, TimestampMixin):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    message = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(32), default='info', nullable=False, index=True)
    link = db.Column(db.String(255), nullable=True)
    is_read = db.Column(db.Boolean, default=False, nullable=False, index=True)
    read_at = db.Column(db.DateTime, nullable=True)

    school = db.relationship('User', foreign_keys=[school_id], backref=db.backref('school_notifications', lazy='dynamic'))

    def mark_read(self):
        self.is_read = True
        self.read_at = utcnow()


class ApprovalRequest(db.Model, TimestampMixin):
    __tablename__ = 'approval_requests'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    request_type = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(32), default='pending', nullable=False, index=True)
    target_model = db.Column(db.String(64), nullable=False, index=True)
    target_id = db.Column(db.String(80), nullable=True, index=True)
    requester_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    reviewer_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    payload = db.Column(db.JSON, nullable=True)
    resolution_notes = db.Column(db.Text, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)

    school = db.relationship('User', foreign_keys=[school_id], backref=db.backref('school_approval_requests', lazy='dynamic'))
    requester = db.relationship('User', foreign_keys=[requester_user_id], backref=db.backref('approval_requests_created', lazy='dynamic'))
    reviewer = db.relationship('User', foreign_keys=[reviewer_user_id], backref=db.backref('approval_requests_reviewed', lazy='dynamic'))

    def approve(self, reviewer, notes=None):
        self.status = 'approved'
        self.reviewer_user_id = getattr(reviewer, 'id', None)
        self.resolved_at = utcnow()
        self.resolution_notes = notes

    def reject(self, reviewer, notes=None):
        self.status = 'rejected'
        self.reviewer_user_id = getattr(reviewer, 'id', None)
        self.resolved_at = utcnow()
        self.resolution_notes = notes


class AIUsageLog(db.Model, TimestampMixin):
    __tablename__ = 'ai_usage_logs'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    feature = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(32), nullable=False, index=True)
    request_units = db.Column(db.Integer, default=0, nullable=False)
    latency_ms = db.Column(db.Integer, nullable=True)
    details = db.Column(db.JSON, nullable=True)

    school = db.relationship('User', foreign_keys=[school_id], backref=db.backref('school_ai_usage_logs', lazy='dynamic'))
    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('user_ai_usage_logs', lazy='dynamic'))


class PlatformJob(db.Model, TimestampMixin):
    __tablename__ = 'platform_jobs'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    job_type = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(32), default='queued', nullable=False, index=True)
    payload = db.Column(db.JSON, nullable=True)
    result = db.Column(db.JSON, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    scheduled_for = db.Column(db.DateTime, nullable=True, index=True)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    school = db.relationship('User', foreign_keys=[school_id], backref=db.backref('school_jobs', lazy='dynamic'))
    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('user_jobs', lazy='dynamic'))


class UserFeedback(db.Model, TimestampMixin):
    __tablename__ = 'user_feedback'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student_details.id'), nullable=True, index=True)
    subject = db.Column(db.String(160), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(32), default='open', nullable=False, index=True)
    responded_at = db.Column(db.DateTime, nullable=True)

    school = db.relationship('User', foreign_keys=[school_id], backref=db.backref('school_feedback_entries', lazy='dynamic'))
    user = db.relationship('User', foreign_keys=[user_id], backref=db.backref('submitted_feedback_entries', lazy='dynamic'))


SOFT_DELETE_MODELS = ()


@event.listens_for(Session, "do_orm_execute")
def _add_soft_delete_criteria(execute_state):
    if (
        not execute_state.is_select
        or execute_state.execution_options.get("include_deleted", False)
    ):
        return

    for model in SOFT_DELETE_MODELS:
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                model,
                lambda cls: cls.is_deleted.is_(False),
                include_aliases=True,
            )
        )


SOFT_DELETE_MODELS = (
    User,
    StudentDetail,
    MealPlan,
    MealTemplate,
)
