-- Удаляем колонку referral_code, так как теперь используем только r_tgid
ALTER TABLE users DROP COLUMN IF EXISTS referral_code;
DROP INDEX IF EXISTS idx_users_referral_code;

