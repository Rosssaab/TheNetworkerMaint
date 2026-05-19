"""Database models for MyNetworkerDev.

Physical table names: ``event_groups``, ``events``, ``event_ticket_types``, etc. (renamed from
legacy ``meeting_*``). Keep this file aligned with the database you connect via ``DATABASE_URL``.
"""

from datetime import datetime

import pyotp
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class Country(db.Model):
    __tablename__ = "countries"

    country_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    country = db.Column(db.String(50), nullable=False)


class Industry(db.Model):
    __tablename__ = "industries"

    industry_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    industry = db.Column(db.String(50), nullable=False)


# NB: the live table is called user_orgainiser_industries (typo in the
# existing schema). We use the Python name `user_industries` everywhere
# in code — the __tablename__ keyword is what ties it to the real table.
user_industries = db.Table(
    "user_orgainiser_industries",
    db.Column("user_id", db.Integer, db.ForeignKey("users.user_id"), primary_key=True),
    db.Column(
        "industry_id",
        db.Integer,
        db.ForeignKey("industries.industry_id"),
        primary_key=True,
    ),
)


class Tag(db.Model):
    __tablename__ = "tags"

    tag_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    industry_id = db.Column(
        db.Integer, db.ForeignKey("industries.industry_id"), nullable=False
    )
    tag = db.Column(db.String(50), nullable=False)

    __table_args__ = (db.Index("ix_tags_industry_id_tag", "industry_id", "tag"),)

    industry = db.relationship("Industry", foreign_keys=[industry_id], lazy="joined")


user_attendee_tags = db.Table(
    "user_attendee_tags",
    db.Column("user_id", db.Integer, db.ForeignKey("users.user_id"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tags.tag_id"), primary_key=True),
)


meeting_group_tags = db.Table(
    "event_group_tags",
    db.Column(
        "meeting_group_id",
        db.Integer,
        db.ForeignKey("event_groups.meeting_group_id"),
        primary_key=True,
    ),
    db.Column("tag_id", db.Integer, db.ForeignKey("tags.tag_id"), primary_key=True),
)


class User(db.Model):
    __tablename__ = "users"

    user_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(50), nullable=False)
    first_name = db.Column(db.String(100), nullable=True)
    second_name = db.Column(db.String(100), nullable=True)
    mobile = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(50), nullable=False, index=True)
    created_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    image_name = db.Column(db.String(50), nullable=False, default="")
    country_id = db.Column(db.Integer, db.ForeignKey("countries.country_id"), nullable=False)
    latitude = db.Column(db.Numeric(9, 6), nullable=True)
    longitude = db.Column(db.Numeric(9, 6), nullable=True)
    verification_send = db.Column(db.DateTime, nullable=True)
    verification_code = db.Column(db.String(50), nullable=True)
    verification_confirmed = db.Column(db.DateTime, nullable=True)
    password_hash = db.Column(db.String(255), nullable=True)
    twofa_enabled = db.Column(db.Boolean, nullable=False, default=False)
    twofa_secret = db.Column(db.String(64), nullable=True)
    admin_user = db.Column("Admin_User", db.Boolean, nullable=False, default=False)
    test_user = db.Column("test_user", db.Boolean, nullable=False, default=False)

    country = db.relationship("Country", foreign_keys=[country_id], lazy="joined")
    industries = db.relationship(
        "Industry",
        secondary=user_industries,
        order_by="Industry.industry",
        lazy="joined",
    )
    attendee_tags = db.relationship(
        "Tag",
        secondary=user_attendee_tags,
        order_by="Tag.tag",
        lazy="joined",
    )
    meeting_groups = db.relationship(
        "MeetingGroup",
        back_populates="owner",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    meeting_attendees = db.relationship(
        "MeetingAttendee",
        back_populates="user",
        lazy="dynamic",
    )
    saved_meetings = db.relationship(
        "UserSavedMeeting",
        back_populates="user",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_verified(self):
        return self.verification_confirmed is not None

    @property
    def is_profile_complete(self):
        """User is considered profile-complete once they have saved a
        location on the map (both latitude and longitude set)."""
        return self.latitude is not None and self.longitude is not None

    # ----- Two-factor authentication (TOTP) -------------------------------

    def generate_twofa_secret(self):
        """Create and store a brand-new base32 TOTP secret."""
        self.twofa_secret = pyotp.random_base32()
        return self.twofa_secret

    def get_twofa_uri(self, issuer="The Networker"):
        """otpauth:// URI that authenticator apps turn into a QR code."""
        if not self.twofa_secret:
            return None
        return pyotp.totp.TOTP(self.twofa_secret).provisioning_uri(
            name=self.email,
            issuer_name=issuer,
        )

    def verify_twofa_code(self, code):
        """Return True if `code` matches the current TOTP window. Allows a
        1-step drift either side so slow phone clocks still succeed."""
        if not self.twofa_secret or not code:
            return False
        try:
            return pyotp.TOTP(self.twofa_secret).verify(
                str(code).strip(), valid_window=1
            )
        except Exception:
            return False


class MeetingGroup(db.Model):
    __tablename__ = "event_groups"

    meeting_group_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    meeting_group_name = db.Column(db.Unicode(180), nullable=False)
    description = db.Column(db.UnicodeText, nullable=True)
    website_url = db.Column(db.Unicode(500), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    image_filename = db.Column(db.Unicode(255), nullable=True)
    meeting_format = db.Column(db.String(10), nullable=False, default="Face2Face")
    industry_id = db.Column(
        db.Integer, db.ForeignKey("industries.industry_id"), nullable=True
    )

    __table_args__ = (
        db.Index("ix_meeting_groups_created_at", "created_at"),
        db.Index("ix_meeting_groups_user_id", "user_id"),
        db.Index("ix_meeting_groups_industry_id", "industry_id"),
        db.Index("ix_meeting_groups_meeting_group_name", "meeting_group_name"),
    )

    owner = db.relationship("User", back_populates="meeting_groups", lazy="joined")
    industry = db.relationship("Industry", foreign_keys=[industry_id], lazy="joined")
    tags = db.relationship(
        "Tag",
        secondary=meeting_group_tags,
        order_by="Tag.tag",
        lazy="selectin",
    )
    meetings = db.relationship(
        "Meeting",
        back_populates="meeting_group",
        lazy="selectin",
        passive_deletes=True,
    )


class Meeting(db.Model):
    __tablename__ = "events"

    meeting_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    meeting_group_id = db.Column(
        db.Integer, db.ForeignKey("event_groups.meeting_group_id"), nullable=False
    )
    creator_user_id = db.Column(db.Integer, nullable=False)
    title = db.Column(db.Unicode(180), nullable=False)
    subject = db.Column(db.UnicodeText, nullable=False)
    starts_at = db.Column(db.DateTime, nullable=True)
    meeting_format = db.Column(db.String(10), nullable=True, default="Face2Face")
    duration_minutes = db.Column(db.Integer, nullable=True, default=60)
    location_city = db.Column(db.Unicode(120), nullable=True)
    location_postcode = db.Column(db.Unicode(20), nullable=True)
    location_country = db.Column(db.Unicode(80), nullable=True)
    venue_name = db.Column(db.Unicode(180), nullable=True)
    website_url = db.Column(db.Unicode(500), nullable=True)
    address_line1 = db.Column(db.Unicode(180), nullable=True)
    address_line2 = db.Column(db.Unicode(180), nullable=True)
    address_town = db.Column(db.Unicode(120), nullable=True)
    address_county = db.Column(db.Unicode(120), nullable=True)
    address_postcode = db.Column(db.Unicode(20), nullable=True)
    address_country = db.Column(db.Unicode(80), nullable=True)
    latitude = db.Column(db.Numeric(9, 6), nullable=True)
    longitude = db.Column(db.Numeric(9, 6), nullable=True)
    virtual_platform = db.Column(db.Unicode(50), nullable=True)
    virtual_link = db.Column(db.Unicode(500), nullable=True)
    is_paid_and_published = db.Column(db.Boolean, nullable=False, default=False)
    waitlist_enabled = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(20), nullable=False, default="Draft")
    image_name = db.Column(db.String(50), nullable=True)
    image_location = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    recurrence_rule_id = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        db.Index("ix_meetings_meeting_group_id_starts_at", "meeting_group_id", "starts_at"),
        db.Index("ix_meetings_starts_at", "starts_at"),
        db.Index("ix_meetings_status", "status"),
    )

    meeting_group = db.relationship("MeetingGroup", back_populates="meetings", lazy="joined")
    ticket_types = db.relationship(
        "MeetingTicketType",
        back_populates="meeting",
        lazy="selectin",
        passive_deletes=True,
    )
    attendees = db.relationship(
        "MeetingAttendee",
        back_populates="meeting",
        lazy="dynamic",
        passive_deletes=True,
    )
    user_saves = db.relationship(
        "UserSavedMeeting",
        back_populates="meeting",
        lazy="dynamic",
        passive_deletes=True,
    )


class UserSavedMeeting(db.Model):
    """Bookmark: a signed-in user saved a meeting from public search/browse."""

    __tablename__ = "user_saved_meetings"

    user_saved_meeting_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    meeting_id = db.Column(db.Integer, db.ForeignKey("events.meeting_id"), nullable=False)
    saved_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("user_id", "meeting_id", name="uq_user_saved_meetings_user_meeting"),
        db.Index("ix_user_saved_meetings_user_id", "user_id"),
        db.Index("ix_user_saved_meetings_meeting_id", "meeting_id"),
    )

    user = db.relationship("User", back_populates="saved_meetings", lazy="joined")
    meeting = db.relationship("Meeting", back_populates="user_saves", lazy="joined")


class MeetingTicketType(db.Model):
    __tablename__ = "event_ticket_types"

    ticket_type_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    meeting_id = db.Column(
        db.Integer, db.ForeignKey("events.meeting_id"), nullable=False
    )
    ticket_name = db.Column(db.Unicode(100), nullable=False)
    ticket_description = db.Column(db.Unicode(500), nullable=True)
    currency_code = db.Column(db.String(3), nullable=False, default="GBP")
    price_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    max_quantity = db.Column(db.Integer, nullable=False)
    max_tickets_per_user = db.Column(db.Integer, nullable=False, default=1)
    sales_open_at = db.Column(db.DateTime, nullable=True)
    sales_close_at = db.Column(db.DateTime, nullable=True)
    vat_rate_percent = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    # none | plus | included — how price_amount relates to UK VAT (20% when applicable)
    vat_treatment = db.Column(db.String(16), nullable=False, default="none")
    refund_policy = db.Column(db.Unicode(200), nullable=True)
    ticket_notes = db.Column(db.Unicode(1000), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="Draft")
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True)

    meeting = db.relationship("Meeting", back_populates="ticket_types", lazy="joined")
    attendees = db.relationship(
        "MeetingAttendee",
        back_populates="ticket_type",
        lazy="dynamic",
        passive_deletes=True,
    )


class MeetingAttendee(db.Model):
    """Attendee ticket bookings (event_attendees table)."""

    __tablename__ = "event_attendees"

    meeting_attendee_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("events.meeting_id"), nullable=False)
    ticket_type_id = db.Column(
        db.Integer, db.ForeignKey("event_ticket_types.ticket_type_id"), nullable=False
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    amount_paid = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="Reserved")
    booked_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (db.Index("ix_meeting_attendees_meeting_id_booked_at", "meeting_id", "booked_at"),)

    meeting = db.relationship("Meeting", back_populates="attendees", lazy="joined")
    ticket_type = db.relationship("MeetingTicketType", back_populates="attendees", lazy="joined")
    user = db.relationship("User", back_populates="meeting_attendees", lazy="joined")
    ticket_entries = db.relationship(
        "MeetingTicketEntry",
        back_populates="attendee",
        order_by="MeetingTicketEntry.meeting_ticket_entry_id",
        # Use lazy "select" (not selectin): selectin would issue a batch query for every
        # MeetingAttendee load site-wide and breaks if event_ticket_entries is absent.
        lazy="select",
        passive_deletes=True,
    )


class MeetingTicketEntry(db.Model):
    """One scannable admission token per ticket seat (face-to-face check-in at the door)."""

    __tablename__ = "event_ticket_entries"

    meeting_ticket_entry_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    meeting_attendee_id = db.Column(
        db.Integer,
        db.ForeignKey("event_attendees.meeting_attendee_id", ondelete="CASCADE"),
        nullable=False,
    )
    entry_token = db.Column(db.String(48), nullable=False, unique=True)

    __table_args__ = (db.Index("ix_meeting_ticket_entries_meeting_attendee_id", "meeting_attendee_id"),)

    attendee = db.relationship("MeetingAttendee", back_populates="ticket_entries")


class UserTransaction(db.Model):
    __tablename__ = "user_transactions"

    user_transaction_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    tx_id = db.Column(db.BigInteger, nullable=False)
    tx_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    description = db.Column(db.Unicode(500), nullable=False)
    currency_code = db.Column(db.String(3), nullable=False, default="GBP")
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    vat = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False)
    vat_rate_percent = db.Column(db.Numeric(5, 2), nullable=True)
    product_type = db.Column(db.String(30), nullable=False, default="other")
    tx_type = db.Column(db.String(20), nullable=False, default="purchase")
    tx_status = db.Column(db.String(20), nullable=False, default="completed")
    meeting_attendee_id = db.Column(
        db.Integer, db.ForeignKey("event_attendees.meeting_attendee_id"), nullable=True
    )
    meeting_group_id = db.Column(
        db.Integer, db.ForeignKey("event_groups.meeting_group_id"), nullable=True
    )
    meeting_id = db.Column(db.Integer, db.ForeignKey("events.meeting_id"), nullable=True)
    payment_reference = db.Column(db.Unicode(120), nullable=True)
    notes = db.Column(db.Unicode(1000), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", "tx_id", name="UQ_user_transactions_user_tx"),
        db.Index("ix_user_transactions_user_id_tx_date", "user_id", "tx_date"),
    )


class PromotionOrder(db.Model):
    __tablename__ = "promotion_orders"

    promotion_order_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_transaction_id = db.Column(
        db.Integer, db.ForeignKey("user_transactions.user_transaction_id"), nullable=False
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    scope = db.Column(db.String(10), nullable=False)
    meeting_group_id = db.Column(
        db.Integer, db.ForeignKey("event_groups.meeting_group_id"), nullable=True
    )
    meeting_id = db.Column(db.Integer, db.ForeignKey("events.meeting_id"), nullable=True)
    package_tier = db.Column(db.SmallInteger, nullable=False)
    price_amount = db.Column(db.Numeric(10, 2), nullable=False)
    currency_code = db.Column(db.String(3), nullable=False, default="GBP")
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True)


class SearchIndexEntity(db.Model):
    """Registry of listings to submit to search engines (populated when promotions activate)."""

    __tablename__ = "search_index_entities"

    search_index_entity_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    entity_type = db.Column(db.String(20), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    title = db.Column(db.Unicode(180), nullable=False)
    plain_text_summary = db.Column(db.UnicodeText, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")
    source = db.Column(db.String(30), nullable=False, default="promotion")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_promoted_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("entity_type", "entity_id", name="uq_search_index_entities_entity"),
        db.Index("ix_search_index_entities_status", "status"),
        db.Index("ix_search_index_entities_owner", "owner_user_id"),
    )


class PromotionCreditLedger(db.Model):
    __tablename__ = "promotion_credit_ledger"

    ledger_id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    delta = db.Column(db.Integer, nullable=False)
    balance_after = db.Column(db.Integer, nullable=False)
    source_type = db.Column(db.String(30), nullable=False)
    user_transaction_id = db.Column(
        db.Integer, db.ForeignKey("user_transactions.user_transaction_id"), nullable=True
    )
    promotion_order_id = db.Column(
        db.Integer, db.ForeignKey("promotion_orders.promotion_order_id"), nullable=True
    )
    bundle_key = db.Column(db.String(40), nullable=True)
    notes = db.Column(db.Unicode(500), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
