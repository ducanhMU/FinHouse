FROM python:3.12-slim-bookworm

USER root

# 1. Đồng bộ Java 17 (để khớp với Airflow) và cài đặt công cụ
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jre-headless \
    curl \
    procps \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Thiết lập biến môi trường đồng bộ
ENV SPARK_VERSION=3.5.3
ENV HADOOP_VERSION=3
ENV SPARK_HOME=/opt/spark
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="${SPARK_HOME}/bin:${SPARK_HOME}/sbin:${PATH}"
ENV PYSPARK_PYTHON=python3
ENV PYSPARK_DRIVER_PYTHON=python3

# 3. Tải Spark với cấu hình "Siêu Retry" (để không bị lỗi 56 như bên Airflow)
RUN curl -L \
    --retry 10 \
    --retry-delay 15 \
    --retry-all-errors \
    --connect-timeout 120 \
    --max-time 1800 \
    "https://archive.apache.org/dist/spark/spark-${SPARK_VERSION}/spark-${SPARK_VERSION}-bin-hadoop${HADOOP_VERSION}.tgz" \
    -o /tmp/spark.tgz && \
    tar -xf /tmp/spark.tgz -C /opt && \
    mv /opt/spark-${SPARK_VERSION}-bin-hadoop${HADOOP_VERSION} ${SPARK_HOME} && \
    rm /tmp/spark.tgz

# 4. Tải JDBC Drivers đồng bộ phiên bản
RUN curl -L --retry 5 --retry-delay 5 \
    -o ${SPARK_HOME}/jars/sqlite-jdbc-3.46.0.0.jar \
    https://repo1.maven.org/maven2/org/xerial/sqlite-jdbc/3.46.0.0/sqlite-jdbc-3.46.0.0.jar && \
    curl -L --retry 5 --retry-delay 5 \
    -o ${SPARK_HOME}/jars/clickhouse-jdbc-0.6.3-all.jar \
    https://repo1.maven.org/maven2/com/clickhouse/clickhouse-jdbc/0.6.3/clickhouse-jdbc-0.6.3-all.jar

# 5. Cấu hình Entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR ${SPARK_HOME}

ENTRYPOINT ["/entrypoint.sh"]