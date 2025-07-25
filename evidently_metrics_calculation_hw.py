import datetime
import io
import logging
import random
import time
import uuid

import joblib
import pandas as pd
import psycopg
import pytz
from evidently import ColumnMapping
from evidently.metrics import (
    ColumnDriftMetric,
    DatasetDriftMetric,
    DatasetMissingValuesMetric,
    ColumnQuantileMetric
)
from evidently.report import Report
from prefect import flow, task

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s"
)

SEND_TIMEOUT = 10
rand = random.Random()

create_table_statement = """
drop table if exists evidently_metrics_hw;
create table evidently_metrics_hw(
	timestamp timestamp,
	prediction_drift float,
	num_drifted_columns integer,
	share_missing_values float,
    fare_amount_quantile05 float
)
"""

reference_data = pd.read_parquet("data/reference.parquet")
with open("models/lin_reg.bin", "rb") as f_in:
    model = joblib.load(f_in)

raw_data = pd.read_parquet("data/green_tripdata_2024-03.parquet")

begin = datetime.datetime(2024, 3, 1, 0, 0)
num_features = ["passenger_count", "trip_distance", "fare_amount", "total_amount"]
cat_features = ["PULocationID", "DOLocationID"]
column_mapping = ColumnMapping(
    prediction="prediction",
    numerical_features=num_features,
    categorical_features=cat_features,
    target=None,
)

report = Report(
    metrics=[
        ColumnDriftMetric(column_name="prediction"),
        DatasetDriftMetric(),
        DatasetMissingValuesMetric(),
        ColumnQuantileMetric(column_name='fare_amount', quantile=0.5, options={})
    ]
)


@task
def prep_db():
    with psycopg.connect(
        "host=localhost port=5432 user=postgres password=example", autocommit=True
    ) as conn:
        res = conn.execute("SELECT 1 FROM pg_database WHERE datname='test'")
        if len(res.fetchall()) == 0:
            logging.info('Creating database: "test"')
            conn.execute("create database test;")
        else:
            logging.info('Using database: "test"')
        with psycopg.connect(
            "host=localhost port=5432 dbname=test user=postgres password=example"
        ) as conn:
            conn.execute(create_table_statement)


@task(cache_policy=None)
def calculate_metrics_postgresql(curr, i):
    current_data = raw_data[
        (raw_data.lpep_pickup_datetime >= (begin + datetime.timedelta(i)))
        & (raw_data.lpep_pickup_datetime < (begin + datetime.timedelta(i + 1)))
    ]

    # current_data.fillna(0, inplace=True)
    current_data["prediction"] = model.predict(
        current_data[num_features + cat_features].fillna(0)
    )

    report.run(
        reference_data=reference_data,
        current_data=current_data,
        column_mapping=column_mapping,
    )

    result = report.as_dict()

    prediction_drift = result["metrics"][0]["result"]["drift_score"]
    num_drifted_columns = result["metrics"][1]["result"]["number_of_drifted_columns"]
    share_missing_values = result["metrics"][2]["result"]["current"]["share_of_missing_values"]
    fare_amount_quantile05 = result['metrics'][3]['result']["current"]["value"]

    ColumnQuantileMetric(column_name='fare_amount', quantile=0.5, options={}),

    curr.execute(
        "insert into evidently_metrics_hw(timestamp, prediction_drift, num_drifted_columns, share_missing_values, fare_amount_quantile05) values (%s, %s, %s, %s, %s)",
        (
            begin + datetime.timedelta(i),
            prediction_drift,
            num_drifted_columns,
            share_missing_values,
            fare_amount_quantile05
        ),
    )


@flow
def batch_monitoring_backfill():
    prep_db()
    last_send = datetime.datetime.now() - datetime.timedelta(seconds=10)
    with psycopg.connect(
        "host=localhost port=5432 dbname=test user=postgres password=example",
        autocommit=True,
    ) as conn:
        for i in range(0, 30):
            with conn.cursor() as curr:
                calculate_metrics_postgresql(curr, i)

            new_send = datetime.datetime.now()
            seconds_elapsed = (new_send - last_send).total_seconds()
            if seconds_elapsed < SEND_TIMEOUT:
                time.sleep(SEND_TIMEOUT - seconds_elapsed)
            while last_send < new_send:
                last_send = last_send + datetime.timedelta(seconds=10)
            logging.info("data sent")


if __name__ == "__main__":
    batch_monitoring_backfill()
