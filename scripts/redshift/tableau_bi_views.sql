-- Curated Tableau read layer. Run after deploying dbt marts and ML model tables.

-- Existing role tableau_analytics_reader is provisioned separately.

CREATE SCHEMA IF NOT EXISTS analytics_bi;

CREATE SCHEMA IF NOT EXISTS analytics_ml;

CREATE OR REPLACE VIEW analytics_ml.churn_features_v1 AS SELECT customer_key,as_of_date,churn_next_30d, CASE WHEN recency_days<=30 THEN '0-30d' WHEN recency_days<=60 THEN '31-60d' WHEN recency_days<=90 THEN '61-90d' ELSE '91+d' END AS recency_bin, CASE WHEN order_count_90d=0 THEN '0' WHEN order_count_90d<=2 THEN '1-2' WHEN order_count_90d<=5 THEN '3-5' ELSE '6+' END AS orders_bin, CASE WHEN spend_90d_gbp<=0 THEN '0' WHEN spend_90d_gbp<200 THEN '(0,200)' WHEN spend_90d_gbp<600 THEN '[200,600)' ELSE '600+' END AS spend_bin, CASE WHEN lifetime_order_count<=1 THEN '1' WHEN lifetime_order_count<=3 THEN '2-3' WHEN lifetime_order_count<=8 THEN '4-8' ELSE '9+' END AS lifetime_bin FROM analytics_marts.mart_customer_churn_training WITH NO SCHEMA BINDING;

CREATE OR REPLACE VIEW analytics_ml.churn_nb_predictions_v1 AS WITH examples AS (SELECT customer_key,as_of_date,churn_next_30d AS actual_label,recency_bin,orders_bin,spend_bin,lifetime_bin,'validation' AS cohort FROM analytics_ml.churn_features_v1 WHERE as_of_date>=DATE '2026-09-01' UNION ALL SELECT customer_key,as_of_date,CAST(NULL AS INTEGER),CASE WHEN recency_days<=30 THEN '0-30d' WHEN recency_days<=60 THEN '31-60d' WHEN recency_days<=90 THEN '61-90d' ELSE '91+d' END, CASE WHEN order_count_90d=0 THEN '0' WHEN order_count_90d<=2 THEN '1-2' WHEN order_count_90d<=5 THEN '3-5' ELSE '6+' END, CASE WHEN spend_90d_gbp<=0 THEN '0' WHEN spend_90d_gbp<200 THEN '(0,200)' WHEN spend_90d_gbp<600 THEN '[200,600)' ELSE '600+' END, CASE WHEN lifetime_order_count<=1 THEN '1' WHEN lifetime_order_count<=3 THEN '2-3' WHEN lifetime_order_count<=8 THEN '4-8' ELSE '9+' END,'current' FROM analytics_marts.mart_customer_churn_scoring), flat AS (SELECT customer_key,as_of_date,actual_label,cohort,'recency' AS feature,recency_bin AS value FROM examples UNION ALL SELECT customer_key,as_of_date,actual_label,cohort,'orders' AS feature,orders_bin AS value FROM examples UNION ALL SELECT customer_key,as_of_date,actual_label,cohort,'spend' AS feature,spend_bin AS value FROM examples UNION ALL SELECT customer_key,as_of_date,actual_label,cohort,'lifetime' AS feature,lifetime_bin AS value FROM examples), posterior AS (SELECT f.customer_key,f.as_of_date,f.actual_label,f.cohort,p.label AS candidate_label,LN(p.prior_probability)+SUM(LN(l.conditional_probability)) AS log_probability FROM flat f CROSS JOIN analytics_ml.churn_nb_priors_v1 p JOIN analytics_ml.churn_nb_likelihoods_v1 l ON l.label=p.label AND l.feature=f.feature AND l.value=f.value GROUP BY f.customer_key,f.as_of_date,f.actual_label,f.cohort,p.label,p.prior_probability), log_odds AS (SELECT customer_key,as_of_date,actual_label,cohort,MAX(CASE WHEN candidate_label=0 THEN log_probability END) AS log_p0,MAX(CASE WHEN candidate_label=1 THEN log_probability END) AS log_p1 FROM posterior GROUP BY 1,2,3,4) SELECT customer_key,as_of_date,actual_label,cohort,CAST(1.0/(1.0+EXP(log_p0-log_p1)) AS DECIMAL(10,6)) AS churn_probability,CASE WHEN log_p1>=log_p0 THEN 1 ELSE 0 END AS predicted_label FROM log_odds WITH NO SCHEMA BINDING;

CREATE OR REPLACE VIEW analytics_ml.churn_nb_validation_metrics_v1 AS WITH v AS (SELECT actual_label,predicted_label,churn_probability FROM analytics_ml.churn_nb_predictions_v1 WHERE cohort='validation'), counts AS (SELECT COUNT(*) AS examples,SUM(CASE WHEN actual_label=1 AND predicted_label=1 THEN 1 ELSE 0 END) AS tp,SUM(CASE WHEN actual_label=0 AND predicted_label=0 THEN 1 ELSE 0 END) AS tn,SUM(CASE WHEN actual_label=0 AND predicted_label=1 THEN 1 ELSE 0 END) AS fp,SUM(CASE WHEN actual_label=1 AND predicted_label=0 THEN 1 ELSE 0 END) AS fn,SUM(actual_label) AS positive_examples FROM v),by_score AS (SELECT churn_probability,SUM(CASE WHEN actual_label=1 THEN 1 ELSE 0 END) AS positives,SUM(CASE WHEN actual_label=0 THEN 1 ELSE 0 END) AS negatives FROM v GROUP BY 1), scored AS (SELECT churn_probability,positives,negatives,COALESCE(SUM(negatives) OVER (ORDER BY churn_probability ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),0) AS neg_before FROM by_score),auc AS (SELECT SUM(positives*(neg_before+0.5*negatives)) AS rank_pairs FROM scored) SELECT c.examples,c.tp,c.tn,c.fp,c.fn,c.positive_examples,ROUND((c.tp+c.tn)::DECIMAL(18,5)/NULLIF(c.examples,0),4) AS accuracy,ROUND(c.tp::DECIMAL(18,5)/NULLIF(c.tp+c.fp,0),4) AS precision,ROUND(c.tp::DECIMAL(18,5)/NULLIF(c.tp+c.fn,0),4) AS recall,ROUND(2.0*c.tp/NULLIF(2.0*c.tp+c.fp+c.fn,0),4) AS f1,ROUND(a.rank_pairs/NULLIF(c.positive_examples*(c.examples-c.positive_examples),0),4) AS roc_auc,ROUND(GREATEST(c.positive_examples,c.examples-c.positive_examples)::DECIMAL(18,5)/NULLIF(c.examples,0),4) AS majority_baseline_accuracy FROM counts c CROSS JOIN auc a WITH NO SCHEMA BINDING;

CREATE OR REPLACE VIEW analytics_ml.churn_nb_score_segments_v1 AS SELECT CASE WHEN churn_probability>=0.8 THEN 'Very high (80%+)' WHEN churn_probability>=0.6 THEN 'High (60-80%)' WHEN churn_probability>=0.4 THEN 'Medium (40-60%)' WHEN churn_probability>=0.2 THEN 'Low (20-40%)' ELSE 'Very low (<20%)' END AS segment,COUNT(*) AS customer_count,ROUND(AVG(churn_probability),4) AS average_predicted_risk FROM analytics_ml.churn_nb_predictions_v1 WHERE cohort='current' GROUP BY 1 WITH NO SCHEMA BINDING;

CREATE OR REPLACE VIEW analytics_ml.tableau_lb_v_churn_risk_by_recency AS
SELECT CASE WHEN s.recency_days<=30 THEN '0-30 days'
WHEN s.recency_days<=60 THEN '31-60 days'
WHEN s.recency_days<=90 THEN '61-90 days' ELSE '91+ days' END AS recency_segment,
COUNT(*) AS customers,ROUND(100.0*AVG(p.churn_probability),2) AS mean_predicted_churn_pct
FROM analytics_ml.churn_nb_predictions_v1 p
JOIN analytics_marts.mart_customer_churn_scoring s
ON s.customer_key=p.customer_key AND s.as_of_date=p.as_of_date
WHERE p.cohort='current' GROUP BY 1 WITH NO SCHEMA BINDING;

CREATE OR REPLACE VIEW analytics_ml.tableau_lb_v_monthly_sales AS
SELECT CAST(DATE_TRUNC('month',order_timestamp_utc) AS DATE) AS sales_month,
source_system,COUNT(*) AS orders,ROUND(SUM(current_gbp),2) AS net_sales_gbp
FROM analytics_marts.fct_orders GROUP BY 1,2 WITH NO SCHEMA BINDING;

CREATE OR REPLACE VIEW analytics_ml.tableau_lb_v_customer_churn_scores AS
SELECT p.customer_key,p.as_of_date,p.churn_probability,
p.predicted_label,
CASE WHEN p.churn_probability>=0.8 THEN 'Very high'
WHEN p.churn_probability>=0.6 THEN 'High'
WHEN p.churn_probability>=0.4 THEN 'Medium'
WHEN p.churn_probability>=0.2 THEN 'Low' ELSE 'Very low' END AS risk_segment,
s.recency_days,s.order_count_90d,s.spend_90d_gbp,s.lifetime_order_count
FROM analytics_ml.churn_nb_predictions_v1 p
JOIN analytics_marts.mart_customer_churn_scoring s
ON s.customer_key=p.customer_key AND s.as_of_date=p.as_of_date
WHERE p.cohort='current' WITH NO SCHEMA BINDING;

CREATE OR REPLACE VIEW analytics_bi.v_churn_model_metrics AS
SELECT examples AS holdout_records,positive_examples AS holdout_churn_events,
tp AS true_positives,tn AS true_negatives,fp AS false_positives,fn AS false_negatives,
accuracy,precision,recall,f1,roc_auc,majority_baseline_accuracy
FROM analytics_ml.churn_nb_validation_metrics_v1;

CREATE OR REPLACE VIEW analytics_bi.v_churn_risk_segments AS
SELECT CASE segment WHEN 'Very low (<20%)' THEN 1 WHEN 'Low (20-40%)' THEN 2
WHEN 'Medium (40-60%)' THEN 3 WHEN 'High (60-80%)' THEN 4 ELSE 5 END AS risk_order,
segment AS risk_segment,customer_count,average_predicted_risk
FROM analytics_ml.churn_nb_score_segments_v1;

CREATE OR REPLACE VIEW analytics_bi.v_churn_monthly_labels AS
SELECT CAST(as_of_date AS DATE) AS cohort_month,
COUNT(*) AS records,SUM(churn_next_30d) AS churn_events,
ROUND(100.0*SUM(churn_next_30d)::DECIMAL(18,5)/NULLIF(COUNT(*),0),2) AS observed_churn_pct
FROM analytics_ml.churn_features_v1 GROUP BY 1;

CREATE OR REPLACE VIEW analytics_bi.v_churn_model_calibration AS
WITH ranked AS (
 SELECT churn_probability,actual_label,NTILE(10) OVER (ORDER BY churn_probability) AS decile
 FROM analytics_ml.churn_nb_predictions_v1 WHERE cohort='validation')
SELECT decile,COUNT(*) AS records,
ROUND(100.0*AVG(churn_probability),2) AS predicted_churn_pct,
ROUND(100.0*SUM(actual_label)::DECIMAL(18,5)/NULLIF(COUNT(*),0),2) AS observed_churn_pct
FROM ranked GROUP BY 1;

CREATE OR REPLACE VIEW analytics_bi.v_churn_risk_by_recency AS SELECT * FROM analytics_ml.tableau_lb_v_churn_risk_by_recency;

CREATE OR REPLACE VIEW analytics_bi.v_monthly_sales AS SELECT * FROM analytics_ml.tableau_lb_v_monthly_sales;

CREATE OR REPLACE VIEW analytics_bi.v_customer_churn_scores AS SELECT * FROM analytics_ml.tableau_lb_v_customer_churn_scores;

GRANT USAGE ON SCHEMA analytics_bi TO ROLE tableau_analytics_reader;

GRANT SELECT ON ALL TABLES IN SCHEMA analytics_bi TO ROLE tableau_analytics_reader;

GRANT SELECT FOR TABLES IN SCHEMA analytics_bi TO ROLE tableau_analytics_reader;
