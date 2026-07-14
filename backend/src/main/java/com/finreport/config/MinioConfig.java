package com.finreport.config;

import io.minio.MinioClient;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * MinIO 客户端配置。
 *
 * <p>创建 MinioClient Bean，用于 PDF 上传/下载、报告产物存储。</p>
 */
@Configuration
public class MinioConfig {

    private static final Logger log = LoggerFactory.getLogger(MinioConfig.class);

    @Value("${minio.endpoint}")
    private String endpoint;

    @Value("${minio.access-key}")
    private String accessKey;

    @Value("${minio.secret-key}")
    private String secretKey;

    @Bean
    public MinioClient minioClient() {
        // MinIO Java Client 8.x 要求 endpoint 包含协议前缀
        String resolvedEndpoint = endpoint.startsWith("http://") || endpoint.startsWith("https://")
                ? endpoint
                : "http://" + endpoint;
        log.debug("[MinioConfig] 初始化 MinIO 客户端 endpoint={}", resolvedEndpoint);
        return MinioClient.builder()
                .endpoint(resolvedEndpoint)
                .credentials(accessKey, secretKey)
                .build();
    }
}
