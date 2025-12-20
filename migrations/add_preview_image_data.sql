-- Добавляем колонку для хранения изображений в базе данных
ALTER TABLE templates ADD COLUMN IF NOT EXISTS preview_image_data BYTEA;
ALTER TABLE templates ADD COLUMN IF NOT EXISTS preview_image_content_type VARCHAR(50);

