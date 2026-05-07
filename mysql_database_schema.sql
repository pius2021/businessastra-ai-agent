-- ═══════════════════════════════════════════════════════
-- OutboundAI — Complete MySQL Database Schema
-- Run this ONCE. Safe to re-run (IF NOT EXISTS everywhere).
-- ═══════════════════════════════════════════════════════

CREATE DATABASE IF NOT EXISTS ai_voice_agent CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE ai_voice_agent;

-- ── Users (Authentication) ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    user_type ENUM('super_admin', 'admin', 'normal_user') DEFAULT 'normal_user',
    is_active TINYINT DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_username (username),
    INDEX idx_email (email)
) CHARACTER SET utf8mb4;

-- ── Customers (Odia loan collection — existing) ───────────────────────────────
CREATE TABLE IF NOT EXISTS customers (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    customer_name   VARCHAR(255) NOT NULL UNIQUE,
    customer_number VARCHAR(50)  NOT NULL,
    loan_amount     VARCHAR(50),
    total_installment       VARCHAR(50),
    cost_per_installment    VARCHAR(50),
    no_of_installment_paid  VARCHAR(50),
    last_installment_paid_on VARCHAR(50),
    installment_left        VARCHAR(50),
    amount_to_be_paid       VARCHAR(50),
    install_due_date        VARCHAR(50),
    fine_for_late_dues      VARCHAR(50),
    call_status ENUM('pending','in_progress','completed','failed') DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) CHARACTER SET utf8mb4;

-- ── Appointments ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS appointments (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    phone VARCHAR(50) NOT NULL,
    date VARCHAR(20) NOT NULL,
    time VARCHAR(10) NOT NULL,
    service VARCHAR(255) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'booked',
    calcom_booking_uid VARCHAR(100),
    created_at VARCHAR(30) NOT NULL
) CHARACTER SET utf8mb4;

-- ── Call logs ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS call_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    phone_number VARCHAR(50),
    customer_name VARCHAR(255),
    lead_name VARCHAR(255),
    room_name VARCHAR(255),
    outcome VARCHAR(50),
    reason TEXT,
    duration_seconds INT,
    recording_url TEXT,
    notes TEXT,
    timestamp VARCHAR(30),
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_phone (phone_number),
    INDEX idx_customer (customer_name),
    INDEX idx_room (room_name)
) CHARACTER SET utf8mb4;

-- ── Settings (BYOK) ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS settings (
    `key` VARCHAR(100) PRIMARY KEY,
    `value` TEXT NOT NULL,
    updated_at VARCHAR(30) NOT NULL
) CHARACTER SET utf8mb4;

-- ── Error / audit logs ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS error_logs (
    id VARCHAR(36) PRIMARY KEY,
    source VARCHAR(50) NOT NULL,
    level VARCHAR(20) NOT NULL DEFAULT 'error',
    message TEXT NOT NULL,
    detail TEXT,
    timestamp VARCHAR(30) NOT NULL
) CHARACTER SET utf8mb4;

-- ── Campaigns ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS campaigns (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    contacts_json LONGTEXT NOT NULL,
    schedule_type VARCHAR(20) NOT NULL DEFAULT 'once',
    schedule_time VARCHAR(10) DEFAULT '09:00',
    call_delay_seconds INT DEFAULT 3,
    system_prompt TEXT,
    agent_profile_id VARCHAR(36),
    created_at VARCHAR(30) NOT NULL,
    last_run_at VARCHAR(30),
    total_dispatched INT DEFAULT 0,
    total_failed INT DEFAULT 0
) CHARACTER SET utf8mb4;

-- ── Contact memory ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contact_memory (
    id VARCHAR(36) PRIMARY KEY,
    phone_number VARCHAR(50) NOT NULL,
    insight TEXT NOT NULL,
    created_at VARCHAR(30) NOT NULL,
    INDEX idx_cm_phone (phone_number)
) CHARACTER SET utf8mb4;

-- ── Agent profiles ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_profiles (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    voice VARCHAR(50) NOT NULL DEFAULT 'Aoede',
    model VARCHAR(100) NOT NULL DEFAULT 'sarvam-30b',
    system_prompt TEXT,
    enabled_tools TEXT DEFAULT '[]',
    is_default TINYINT DEFAULT 0,
    created_at VARCHAR(30) NOT NULL
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS uploaded_lists (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    source_filename VARCHAR(255),
    columns_json LONGTEXT NOT NULL,
    phone_column VARCHAR(255),
    lead_name_column VARCHAR(255),
    row_count INT DEFAULT 0,
    created_at VARCHAR(30) NOT NULL,
    updated_at VARCHAR(30) NOT NULL
) CHARACTER SET utf8mb4;

CREATE TABLE IF NOT EXISTS uploaded_list_rows (
    id VARCHAR(36) PRIMARY KEY,
    list_id VARCHAR(36) NOT NULL,
    row_index INT NOT NULL,
    row_json LONGTEXT NOT NULL,
    phone_number VARCHAR(50),
    lead_name VARCHAR(255),
    call_status VARCHAR(30) DEFAULT 'pending',
    last_call_room VARCHAR(255),
    last_call_at VARCHAR(30),
    call_error TEXT,
    created_at VARCHAR(30) NOT NULL,
    INDEX idx_uploaded_rows_list (list_id),
    INDEX idx_uploaded_rows_phone (phone_number)
) CHARACTER SET utf8mb4;

-- ── Safe migration: add columns if missing ────────────────────────────────────

-- call_status on customers
SET @sql = (SELECT IF(
    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='customers' AND COLUMN_NAME='call_status') = 0,
    'ALTER TABLE customers ADD COLUMN call_status ENUM(''pending'',''in_progress'',''completed'',''failed'') DEFAULT ''pending''',
    'SELECT 1'
));
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- recording_url on call_logs
SET @sql2 = (SELECT IF(
    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_logs' AND COLUMN_NAME='recording_url') = 0,
    'ALTER TABLE call_logs ADD COLUMN recording_url TEXT',
    'SELECT 1'
));
PREPARE stmt FROM @sql2; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- notes on call_logs
SET @sql3 = (SELECT IF(
    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='call_logs' AND COLUMN_NAME='notes') = 0,
    'ALTER TABLE call_logs ADD COLUMN notes TEXT',
    'SELECT 1'
));
PREPARE stmt FROM @sql3; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ── Conversations ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_conversations (
    id VARCHAR(36) PRIMARY KEY,
    room_name VARCHAR(255) NOT NULL,
    speaker VARCHAR(50) NOT NULL,
    text_content TEXT NOT NULL,
    timestamp VARCHAR(30) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_conv_room (room_name)
) CHARACTER SET utf8mb4;

SELECT 'OutboundAI schema ready ✅' AS status;
