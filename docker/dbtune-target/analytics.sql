SELECT customer_id, region_id, SUM(amount) AS revenue, COUNT(*) AS purchases
FROM sales_events
WHERE created_at >= NOW() - INTERVAL '30 days'
GROUP BY customer_id, region_id
ORDER BY revenue DESC
LIMIT 100;
