from flask_sqlalchemy import SQLAlchemy
from datetime import date
from flask_login import UserMixin
from werkzeug.security import check_password_hash as check_legacy_password_hash

from .extensions import bcrypt

db = SQLAlchemy()

# ---------------- The Common Table for All Logins ----------------
class User(db.Model, UserMixin):
    __tablename__ = 'users'
    _BCRYPT_PREFIXES = ('$2a$', '$2b$', '$2y$')
    _LEGACY_HASH_PREFIXES = ('pbkdf2:', 'scrypt:')

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, index=True) # 'student', 'school', 'admin'
    school_name = db.Column(db.String(120), nullable=True, index=True) # For users with 'school' role

    # One-to-one link from a User to their StudentDetail record
    student_detail = db.relationship('StudentDetail', backref='user', uselist=False, cascade="all, delete-orphan", foreign_keys='StudentDetail.user_id')

    # One-to-many link from a school User to all their students
    students = db.relationship('StudentDetail', backref='school', lazy='dynamic', cascade="all, delete-orphan", foreign_keys='StudentDetail.school_id')
    
    # One-to-many link from a school User to all their meal plans
    meal_plans = db.relationship('MealPlan', backref='school_user', lazy='dynamic', cascade="all, delete-orphan", foreign_keys='MealPlan.school_id')

    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

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

# ---------------- For Student-Specific Data (No Login Info) ----------------
class StudentDetail(db.Model):
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
    health_metrics = db.relationship('HealthMetric', backref='student_detail', lazy='dynamic', cascade="all, delete-orphan")
    attendance_records = db.relationship('Attendance', backref='student_detail', lazy='dynamic', cascade="all, delete-orphan")

    @property
    def age(self):
        today = date.today()
        return today.year - self.dob.year - ((today.month, today.day) < (self.dob.month, self.dob.day))

    @property
    def latest_health_metric(self):
        """Returns the most recent health metric record."""
        return self.health_metrics.order_by(HealthMetric.record_date.desc()).first()

    @property
    def latest_height(self):
        """Returns the latest recorded height in cm."""
        metric = self.latest_health_metric
        return metric.height_cm if metric else None

    @property
    def latest_weight(self):
        """Returns the latest recorded weight in kg."""
        metric = self.latest_health_metric
        return metric.weight_kg if metric else None

    @property
    def bmi(self):
        """Calculates BMI from the latest height and weight."""
        height = self.latest_height
        weight = self.latest_weight
        if height and weight and height > 0:
            # Formula: BMI = kg / (m^2)
            height_in_meters = height / 100
            return weight / (height_in_meters ** 2)
        return None

# ---------------- Historical Health & Attendance Data ----------------
class HealthMetric(db.Model):
    __tablename__ = 'health_metrics'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student_details.id'), nullable=False, index=True)
    record_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    height_cm = db.Column(db.Float, nullable=False)
    weight_kg = db.Column(db.Float, nullable=False)
    __table_args__ = (
        db.Index('ix_health_metrics_student_date', 'student_id', 'record_date'),
    )

class Attendance(db.Model):
    __tablename__ = 'attendance'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student_details.id'), nullable=False, index=True)
    attendance_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    
    # Boolean fields for each meal for detailed tracking
    ate_breakfast = db.Column(db.Boolean, default=False, nullable=False)
    ate_lunch = db.Column(db.Boolean, default=False, nullable=False)
    ate_dinner = db.Column(db.Boolean, default=False, nullable=False)
    __table_args__ = (
        db.UniqueConstraint('student_id', 'attendance_date', name='uq_attendance_student_date'),
        db.Index('ix_attendance_student_date', 'student_id', 'attendance_date'),
    )

    @property
    def was_present(self):
        """Checks if the student ate any meal on this day."""
        return self.ate_breakfast or self.ate_lunch or self.ate_dinner

# ---------------- Meal & Food Models ----------------
class Food(db.Model):
    __tablename__ = 'food'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True, index=True)
    calories = db.Column(db.Float, nullable=False)
    protein = db.Column(db.Float, nullable=False)
    carbs = db.Column(db.Float, nullable=False)
    fats = db.Column(db.Float, nullable=False)

class MealPlan(db.Model):
    __tablename__ = 'meal_plan'
    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    plan_date = db.Column(db.Date, nullable=False, index=True)
    items = db.relationship('MealPlanItem', backref='plan', lazy='selectin', cascade="all, delete-orphan")
    __table_args__ = (
        db.UniqueConstraint('school_id', 'plan_date', name='uq_meal_plan_school_date'),
        db.Index('ix_meal_plan_school_date', 'school_id', 'plan_date'),
    )

class MealPlanItem(db.Model):
    __tablename__ = 'meal_plan_item'
    id = db.Column(db.Integer, primary_key=True)
    meal_plan_id = db.Column(db.Integer, db.ForeignKey('meal_plan.id'), nullable=False, index=True)
    food_id = db.Column(db.Integer, db.ForeignKey('food.id'), nullable=False, index=True)
    meal_type = db.Column(db.String(20), nullable=False, index=True)
    food = db.relationship('Food')
    __table_args__ = (
        db.Index('ix_meal_plan_item_plan_food', 'meal_plan_id', 'food_id'),
    )
