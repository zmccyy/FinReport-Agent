package com.finreport.config;

import org.flywaydb.core.Flyway;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnClass;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * Flyway migration configuration for R2DBC applications.
 *
 * <p>Spring Boot's JDBC-based Flyway auto-configuration does not create a JDBC data source when
 * this service only enables R2DBC. This explicit configuration executes the existing Flyway
 * migrations during startup, before the backend begins serving requests.</p>
 */
@Configuration(proxyBeanMethods = false)
@ConditionalOnClass(Flyway.class)
@ConditionalOnProperty(prefix = "spring.flyway", name = "enabled", havingValue = "true", matchIfMissing = true)
public class FlywayMigrationConfiguration {

    /**
     * Creates and migrates the JDBC Flyway schema used alongside R2DBC repositories.
     *
     * @param url JDBC database URL
     * @param username database user name
     * @param password database password
     * @param location classpath migration location
     * @return migrated Flyway instance
     */
    @Bean
    public Flyway flyway(
            @Value("${spring.datasource.url}") String url,
            @Value("${spring.datasource.username}") String username,
            @Value("${spring.datasource.password}") String password,
            @Value("${spring.flyway.locations:classpath:db/migration}") String location) {
        Flyway flyway = Flyway.configure()
                .dataSource(url, username, password)
                .locations(location)
                .baselineOnMigrate(true)
                .baselineVersion("0")
                .load();
        flyway.migrate();
        return flyway;
    }
}
