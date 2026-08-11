-- Lessons-learned inventory: how many operational lessons exist and their
-- lifecycle state (a 30-day TTL forces each lesson to graduate or archive).
SELECT status, COUNT(*) AS lessons,
       MIN(created_date) AS oldest, MAX(created_date) AS newest
FROM `raw.lessons`
GROUP BY status;
