#!/bin/bash
set -e

case "$SPARK_MODE" in
  master)
    exec "$SPARK_HOME/bin/spark-class" org.apache.spark.deploy.master.Master \
      --host 0.0.0.0 \
      --port 7077 \
      --webui-port 8080
    ;;
  worker)
    exec "$SPARK_HOME/bin/spark-class" org.apache.spark.deploy.worker.Worker \
      --webui-port 8081 \
      "$SPARK_MASTER_URL"
    ;;
  *)
    exec "$@"
    ;;
esac
