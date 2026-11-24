# Graph Report - mobihome  (2026-11-16)

## Corpus Check
- 119 files · ~58,310 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 6 file(s) not represented in the graph (top: (none) 2, .bat 2, .example 1)

## Summary
- 1161 nodes · 2273 edges · 81 communities (42 shown, 39 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 203 edges (avg confidence: 0.92)
- Token cost: 513,707 input · 0 output

## Community Hubs (Navigation)
- Social Auth & User Profile
- LiqPay Client & Payment Checks
- Security Audit Findings
- Accessibility Platform Concept
- Schema Migrations
- Auditor Form Validation
- Settings & Env Config
- Home Page & Admin Panels UI
- Payment Views & Callback
- Listing Photos & Display Tests
- Forms (Audit, Register, Review)
- CI Workflow
- LiqPay Callback Tests
- URL Routing & Views
- Checkout & Config Tests
- Django Admin Registrations
- Listing Access Tests
- Placement Payment Logic
- Transition & Integrity Commands
- Accessibility Scoring & Detail
- User Admin & PWA Manifest
- Payment Model & Sync Command
- Tariff & Admin Placement Tests
- LiqPay Hardening Tests A1-A4
- Test Helpers & Fixtures
- Visibility & Favorites Tests
- Property Model
- Amenity Feature Migrations
- Concurrency & Honesty Tests
- Open Payment & Owner Tests
- Model Enums & Signals
- Admin Moderation Views
- Property Admin Actions
- Login Throttling & Auth Page
- Payment Flow Templates
- Favorites & Consultation API
- Placement Activation Rules
- Add Listing Form
- Financial Integrity Tests
- Home & Catalog Views
- Public Property QuerySet
- Placement Payment Migrations
- Production Mode Tests
- Payment Admin (Read-only)
- Superuser Command
- Hero Image
- Payment Event Admin
- Placement Transition Admin
- Integrity Report Command
- Auth Session Model
- Transition Period Tests
- Payment Inline
- Context Processors
- Consultation Request Model
- House Icon SVGs
- Hero Image v2
- Brand Logo
- Archived Filter
- Payment Event Inline
- Placement Status Filter
- User Payment Inline
- Public Profile Page
- Password Reset Templates
- Team Portrait Photo
- Service Worker
- Login & Register Templates
- Privacy Policy
- gunicorn dep
- Pillow dep
- Count-up Animation
- SW Registration
- Logout Confirm
- Delete Confirm
- Reset Complete
- Reset Done

## God Nodes (most connected - your core abstractions)
1. `Property` - 56 edges
2. `make_property()` - 53 edges
3. `create_payment()` - 48 edges
4. `make_user()` - 48 edges
5. `Payment` - 33 edges
6. `PaymentStatus` - 30 edges
7. `callback_body()` - 30 edges
8. `CallbackTests` - 26 edges
9. `ModerationStatus` - 25 edges
10. `make_admin()` - 24 edges

## Surprising Connections (you probably didn't know these)
- `Hard-coded 'Compliant' statuses and default measurement values` --semantically_similar_to--> `H5: Fake Unsplash photos for listings without photos`  [INFERRED] [semantically similar]
  templates/properties/passport_pdf.html → PROJECT_AUDIT.md
- `Team section (Dominik Yaremchuk CEO, Yuliia Koval COO, Andrii Moroz CTO)` --conceptually_related_to--> `H5: Fake Unsplash photos for listings without photos`  [AMBIGUOUS]
  templates/properties/about.html → PROJECT_AUDIT.md
- `AccessibilityAuditInline` --uses--> `AccessibilityAudit`  [INFERRED]
  properties/admin.py → properties/models.py
- `UserProfileInline` --uses--> `UserProfile`  [INFERRED]
  properties/admin.py → properties/models.py
- `Social login (Google, Telegram widget)` --conceptually_related_to--> `C2-C6: Social auth / OTP account takeover (Google, Diia, Telegram, OTP)`  [INFERRED]
  templates/properties/auth.html → PROJECT_AUDIT.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Property Comparison Flow (localStorage, max 4)** — templates_properties_home_getcomparelist, templates_properties_home_updatecompareui, templates_properties_home_togglecompare, templates_properties_home_clearcompare, templates_properties_home_floating_compare_bar, templates_properties_home_mobihome_compare_storage [EXTRACTED 1.00]
- **Quick View Preview Flow** — templates_properties_home_openquickview, templates_properties_home_rendermodalcontent, templates_properties_home_qvescape, templates_properties_home_qvsafeurl, templates_properties_home_quick_view_endpoint, templates_properties_home_quick_view_modal [EXTRACTED 1.00]
- **Listing moderation flow** — templates_properties_property_detail_moderation_status_banner, templates_properties_profile_admin_moderation_panel, templates_properties_profile_my_listings [INFERRED 0.85]
- **Accessibility audit request and display** — templates_properties_property_detail_accessibility_audit_section, templates_properties_profile_audit_requests_panel, templates_properties_index_accessibility_audit, templates_properties_index_accessibility_levels [INFERRED 0.85]
- **Accessibility audit parameters (entrance, lift, thresholds, turning radius, bathroom)** — templates_properties_auditor_form_audit_fieldsets, templates_properties_passport_pdf_audit_checklist, templates_properties_compare_comparison_table [INFERRED 0.85]
- **Critical auth/payment security findings** — project_audit_c2_c6_social_auth_account_takeover, project_audit_c7_fake_card_payment_form, project_audit_c8_promote_listing_idor, project_audit_c9_moderation_bypass [EXTRACTED 1.00]
- **Password reset flow** — templates_registration_password_reset_form, templates_registration_password_reset_done, templates_registration_password_reset_email, templates_registration_password_reset_subject, templates_registration_password_reset_confirm, templates_registration_password_reset_complete [INFERRED 0.95]
- **LiqPay placement payment flow** — templates_properties_payment, templates_properties_liqpay_redirect, templates_properties_payment_result, templates_properties_payment_open_payment_guard [INFERRED 0.95]
- **Account auth pages** — templates_properties_login, templates_properties_register, templates_properties_logout_confirm, templates_registration_password_reset_form [INFERRED 0.75]
- **Hero house design features** — static_img_hero_panoramic_glazed_gable, static_img_hero_dark_facade_wood_accents, static_img_hero_terrace_landscaping [INFERRED 0.75]

## Communities (81 total, 39 thin omitted)

### Community 0 - "Social Auth & User Profile"
Cohesion: 0.06
Nodes (26): AuthProvider, UserProfile, Meta, _decode_jwt_payload(), diia_disabled(), _exchange_google_code(), _get_safe_next_url(), google_callback() (+18 more)

### Community 1 - "LiqPay Client & Payment Checks"
Cohesion: 0.05
Nodes (28): PropertiesConfig, payment_settings_check(), _absolute(), build_checkout(), decode_data(), encode_data(), fetch_status(), handle_callback() (+20 more)

### Community 2 - "Security Audit Findings"
Cohesion: 0.05
Nodes (50): C10: AppConfig.ready() auto makemigrations/migrate + auto-approve, C11: Hardcoded SECRET_KEY/DEBUG/ALLOWED_HOSTS, C12: Admin actions NameError timezone, C1: Migration 0008 fails on clean DB (Feature.slug UNIQUE), C2-C6: Social auth / OTP account takeover (Google, Diia, Telegram, OTP), C7: Fake payment form collecting PAN/CVV (PCI DSS violation), C8: promote_listing IDOR (no owner check), C9: Moderation bypass via free publish (+42 more)

### Community 3 - "Accessibility Platform Concept"
Cohesion: 0.05
Nodes (42): Search filters: lift width >80cm, shower drain, no thresholds, Auditor form (cm measurement validation), Доступний Дім (platform), run.bat quick start, Stack: Django 5, SQLite, Bootstrap 5, total_score (average of audit points), Django>=5.0,<6, Base Layout Template (base.html) (+34 more)

### Community 4 - "Schema Migrations"
Cohesion: 0.06
Nodes (20): Migration, Migration, Migration, Migration, Migration, Migration, Migration, Migration (+12 more)

### Community 5 - "Auditor Form Validation"
Cohesion: 0.06
Nodes (11): AccessibilityAuditForm, Meta, Command, Command, AccessibilityAudit, Meta, BathroomType, Feature (+3 more)

### Community 6 - "Settings & Env Config"
Cohesion: 0.07
Nodes (5): _load_env_file(), main(), main(), liqpay_problems(), startup_problems()

### Community 7 - "Home Page & Admin Panels UI"
Cohesion: 0.08
Nodes (32): Accessibility audit (score out of 10), Accessibility levels 1-5, Advantages cards section, animateCounters() IntersectionObserver, FAQ accordion, Featured listings cards, Hero section, Animated stats counters (+24 more)

### Community 8 - "Payment Views & Callback"
Cohesion: 0.09
Nodes (12): payments_active(), _bump(), cancel_payment(), liqpay_callback(), _own_payment_or_404(), payment_result(), _record_invalid_callback(), start_payment() (+4 more)

### Community 9 - "Listing Photos & Display Tests"
Cohesion: 0.09
Nodes (7): PropertyPhoto, Meta, make_image(), AddListingFormTests, GalleryAndCardTests, NoFakePhotosTests, _with_photos()

### Community 10 - "Forms (Audit, Register, Review)"
Cohesion: 0.08
Nodes (14): AuditRequestForm, Meta, RegisterForm, Meta, ReviewForm, Meta, UserProfileForm, Meta (+6 more)

### Community 11 - "CI Workflow"
Cohesion: 0.07
Nodes (30): DB_ENGINE, DB_NAME, jobs, quality, tests, name, on, pull_request (+22 more)

### Community 12 - "LiqPay Callback Tests"
Cohesion: 0.15
Nodes (3): callback_body(), CallbackTests, A2CallbackAbuseTests

### Community 13 - "URL Routing & Views"
Cohesion: 0.08
Nodes (9): about(), accessibility_levels(), coming_soon(), contact(), faq(), login_view(), logout_view(), privacy_policy() (+1 more)

### Community 14 - "Checkout & Config Tests"
Cohesion: 0.10
Nodes (4): CheckoutTests, ConfigChecksTests, ParallelCallbackTests, PlacementBusinessFlowTests

### Community 15 - "Django Admin Registrations"
Cohesion: 0.10
Nodes (15): AccessibilityAuditAdmin, AccessibilityAuditInline, AuditRequestAdmin, AuthSessionAdmin, ConsultationRequestAdmin, FeatureAdmin, ProfileReviewAdmin, PropertyFavoriteAdmin (+7 more)

### Community 16 - "Listing Access Tests"
Cohesion: 0.09
Nodes (5): AdminPanelTests, FakePaymentRemovedTests, MigrationAndSettingsTests, ModerationFlowTests, SmokePagesTests

### Community 17 - "Placement Payment Logic"
Cohesion: 0.21
Nodes (3): create_payment(), _pay(), PlacementPaymentTests

### Community 18 - "Transition & Integrity Commands"
Cohesion: 0.15
Nodes (5): Command, PlacementTransition, Meta, apply_transition(), transition_candidates()

### Community 19 - "Accessibility Scoring & Detail"
Cohesion: 0.10
Nodes (12): EntranceStatus, score_to_status(), compare_properties(), download_passport(), _is_staff(), property_detail(), get_status_display(), _property_photo_urls() (+4 more)

### Community 20 - "User Admin & PWA Manifest"
Cohesion: 0.11
Nodes (9): UserAdmin, background_color, description, display, icons, name, short_name, start_url (+1 more)

### Community 21 - "Payment Model & Sync Command"
Cohesion: 0.14
Nodes (7): expire_stale(), Command, Payment, Meta, PaymentStatus, mark_reversed(), mark_unsuccessful()

### Community 22 - "Tariff & Admin Placement Tests"
Cohesion: 0.12
Nodes (4): Tariff, Meta, AdminPlacementTests, TariffTests

### Community 23 - "LiqPay Hardening Tests A1-A4"
Cohesion: 0.13
Nodes (5): A1StatusNetworkErrorsTests, A3FailFastTests, A4StrictSignatureTests, A7VersionAlgorithmTests, ConcurrentPaymentCreationTests

### Community 25 - "Visibility & Favorites Tests"
Cohesion: 0.16
Nodes (4): make_user(), UnpublishedVisibilityTests, LegacyTopTests, LogoutTests

### Community 26 - "Property Model"
Cohesion: 0.12
Nodes (4): Property, Meta, payment_paid(), promote_listing()

### Community 27 - "Amenity Feature Migrations"
Cohesion: 0.19
Nodes (7): add_features(), Migration, _unique_slug(), ensure_features(), Migration, Migration, update_features()

### Community 28 - "Concurrency & Honesty Tests"
Cohesion: 0.16
Nodes (4): make_property(), fake(), worker(), DetailHonestyAndSimilarTests

### Community 30 - "Model Enums & Signals"
Cohesion: 0.16
Nodes (6): AuditRequestStatus, AuthSessionStatus, AuthSessionType, ConsultationRequestStatus, ConsultationServiceType, create_or_update_user_profile()

### Community 31 - "Admin Moderation Views"
Cohesion: 0.15
Nodes (6): admin_approve_property(), admin_reject_property(), admin_update_audit_request(), admin_verify_photos(), _can_manage_property(), delete_property()

### Community 33 - "Login Throttling & Auth Page"
Cohesion: 0.29
Nodes (8): _throttled_admin_login(), _client_ip(), _keys(), login_is_locked(), register_login_failure(), reset_login_failures(), auth_page(), _prepare_login_form()

### Community 34 - "Payment Flow Templates"
Cohesion: 0.20
Nodes (8): liqpay_redirect.html template, LiqPay checkout form (data + signature), payment.html template (listing publication), Free submission to moderation, payment_result.html template, Payment statuses: success, failure, cancelled, expired, reversed, pending, Placement tariff (price, duration_days), terms_of_service.html template

### Community 35 - "Favorites & Consultation API"
Cohesion: 0.18
Nodes (5): PropertyFavorite, Meta, submit_consultation_request(), toggle_favorite(), track_click()

### Community 36 - "Placement Activation Rules"
Cohesion: 0.24
Nodes (6): _activate(), activate_on_publication(), _apply_period_rules(), is_live(), is_publishable(), activate_paid_placement_on_publication()

### Community 37 - "Add Listing Form"
Cohesion: 0.22
Nodes (3): AddListingForm, Meta, add_listing()

### Community 39 - "Home & Catalog Views"
Cohesion: 0.22
Nodes (5): _assign_listing_photo_urls(), home(), index(), _no_photo_url(), _similar_listings()

### Community 45 - "Hero Image"
Cohesion: 0.47
Nodes (6): Dark Facade with Warm Wood Accents, Forest / Suburban Country-House Setting, Hero Image (modern single-storey house render), Modern Scandinavian/Barnhouse Architecture, Panoramic Glazed Gable Facade, Terrace with Steps and Landscaped Shrubs

### Community 54 - "House Icon SVGs"
Cohesion: 0.67
Nodes (4): Favicon: filled terracotta house icon, House glyph brand motif, Logo: outline house icon (currentColor stroke), No-photo placeholder (Фото відсутнє)

### Community 55 - "Hero Image v2"
Cohesion: 0.67
Nodes (4): Forest / Suburban Private House Setting, Hero Image v2 (Modern Single-Storey House), Modern Barnhouse / Scandinavian Architecture Style, Panoramic Floor-to-Ceiling Gable Glazing

### Community 56 - "Brand Logo"
Cohesion: 0.50
Nodes (4): mobi home (brand), mobi home Logo, Residential Real Estate Imagery (houses + high-rise apartments), Teal / Navy Brand Color Palette

### Community 61 - "Public Profile Page"
Cohesion: 0.67
Nodes (3): Leave review form (rating, text), public_profile.html (Public user profile + reviews), User listings list

### Community 62 - "Password Reset Templates"
Cohesion: 0.67
Nodes (3): password_reset_confirm.html template, password_reset_email.html template, password_reset_form.html template

## Ambiguous Edges - Review These
- `trackPhoneClick()` → `Author live analytics dashboard (hardcoded figures)`  [AMBIGUOUS]
  templates/properties/property_detail.html · relation: shares_data_with
- `H5: Fake Unsplash photos for listings without photos` → `Team section (Dominik Yaremchuk CEO, Yuliia Koval COO, Andrii Moroz CTO)`  [AMBIGUOUS]
  templates/properties/about.html · relation: conceptually_related_to
- `Payment statuses: success, failure, cancelled, expired, reversed, pending` → `Paid services: full prepayment, no refunds for activated services`  [AMBIGUOUS]
  templates/properties/terms_of_service.html · relation: conceptually_related_to

## Knowledge Gaps
- **55 isolated node(s):** `Catalog Pagination (preserves filter params)`, `Filter Apply Button Dirty-State Snapshot Script`, `/property/<id>/quick-view/ JSON endpoint`, `Django Messages Alerts (auto-dismiss 5s)`, `Theme Init from localStorage` (+50 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 449 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **39 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Property` connect `Property Model` to `LiqPay Client & Payment Checks`, `Auditor Form Validation`, `Payment Views & Callback`, `Listing Photos & Display Tests`, `Forms (Audit, Register, Review)`, `URL Routing & Views`, `Checkout & Config Tests`, `Django Admin Registrations`, `Listing Access Tests`, `Placement Payment Logic`, `Transition & Integrity Commands`, `Accessibility Scoring & Detail`, `Tariff & Admin Placement Tests`, `Test Helpers & Fixtures`, `Visibility & Favorites Tests`, `Concurrency & Honesty Tests`, `Model Enums & Signals`, `Admin Moderation Views`, `Favorites & Consultation API`, `Placement Activation Rules`, `Add Listing Form`, `Financial Integrity Tests`, `Home & Catalog Views`, `Transition Period Tests`?**
  _High betweenness centrality (0.088) - this node is a cross-community bridge._
- **Are the 34 inferred relationships involving `Property` (e.g. with `UserPropertyInline` and `AddListingForm`) actually correct?**
  _`Property` has 34 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Catalog Pagination (preserves filter params)`, `Filter Apply Button Dirty-State Snapshot Script`, `/property/<id>/quick-view/ JSON endpoint` to the rest of the system?**
  _55 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Social Auth & User Profile` be split into smaller, more focused modules?**
  _Cohesion score 0.057124310288867254 - nodes in this community are weakly interconnected._
- **Why does `Payment` connect `Payment Model & Sync Command` to `LiqPay Client & Payment Checks`, `Placement Activation Rules`, `Financial Integrity Tests`, `Payment Views & Callback`, `LiqPay Callback Tests`, `Checkout & Config Tests`, `Django Admin Registrations`, `Placement Payment Logic`, `Transition & Integrity Commands`, `Payment Inline`, `LiqPay Hardening Tests A1-A4`, `Test Helpers & Fixtures`, `User Payment Inline`, `Open Payment & Owner Tests`, `Model Enums & Signals`?**
  _High betweenness centrality (0.049) - this node is a cross-community bridge._

### Low-confidence Hints
_AMBIGUOUS edges — the extractor was unsure. Verify before acting on these._

- **What is the exact relationship between `trackPhoneClick()` and `Author live analytics dashboard (hardcoded figures)`?**
  _Edge tagged AMBIGUOUS (relation: shares_data_with) - confidence is low._
- **What is the exact relationship between `H5: Fake Unsplash photos for listings without photos` and `Team section (Dominik Yaremchuk CEO, Yuliia Koval COO, Andrii Moroz CTO)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._