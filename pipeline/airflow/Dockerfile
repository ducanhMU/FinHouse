FROM apache/airflow:2.10.5

USER root

# 1. Cài đặt Java và công cụ
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jre-headless \
    curl \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Thiết lập biến môi trường
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV SPARK_VERSION=3.3.2
ENV HADOOP_VERSION=3
ENV SPARK_HOME=/opt/spark
ENV PATH="${SPARK_HOME}/bin:${PATH}"

# 3. Tải Spark với cấu hình "Siêu Retry"
# --retry 10: Thử lại tới 10 lần
# --retry-all-errors: Thử lại với bất kỳ lỗi nào (kể cả lỗi 56, 52, v.v.)
# --retry-delay 15: Đợi 15s giữa mỗi lần thử để tránh bị server chặn
# --max-time 1800: Tổng thời gian tải tối đa là 30 phút
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

# 4. Tải JDBC Drivers (Cũng thêm retry cho chắc chắn)
RUN curl -L --retry 5 --retry-delay 5 \
    -o ${SPARK_HOME}/jars/sqlite-jdbc-3.46.0.0.jar \
    https://repo1.maven.org/maven2/org/xerial/sqlite-jdbc/3.46.0.0/sqlite-jdbc-3.46.0.0.jar && \
    curl -L --retry 5 --retry-delay 5 \
    -o ${SPARK_HOME}/jars/clickhouse-jdbc-0.6.3-all.jar \
    https://repo1.maven.org/maven2/com/clickhouse/clickhouse-jdbc/0.6.3/clickhouse-jdbc-0.6.3-all.jar

# 5. Phân quyền
RUN chown -R airflow:root ${SPARK_HOME}

USER airflow

# 6. Cài đặt Python Provider
RUN pip install --no-cache-dir apache-airflow-providers-apache-spark==4.11.1