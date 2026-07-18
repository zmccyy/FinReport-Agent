package com.finreport.config;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.core.AcknowledgeMode;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.rabbit.config.SimpleRabbitListenerContainerFactory;
import org.springframework.amqp.rabbit.listener.RabbitListenerContainerFactory;
import org.springframework.amqp.support.converter.Jackson2JsonMessageConverter;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * RabbitMQ 配置。
 *
 * <p>生产者用 Jackson 序列化消息体；消费者容器设 prefetch=1（spec §3.1 显存限流）。
 * 拓扑声明由 M1.06 scripts/declare_mq.py 或 definitions.json 负责。</p>
 */
@Configuration
public class RabbitMqConfig {

    private static final Logger log = LoggerFactory.getLogger(RabbitMqConfig.class);

    /**
     * JSON 消息转换器 — 避免默认的 Java 序列化。
     */
    @Bean
    public Jackson2JsonMessageConverter messageConverter() {
        return new Jackson2JsonMessageConverter();
    }

    /**
     * RabbitTemplate — 生产者发送消息使用 JSON 转换。
     */
    @Bean
    public RabbitTemplate rabbitTemplate(ConnectionFactory connectionFactory) {
        RabbitTemplate template = new RabbitTemplate(connectionFactory);
        template.setMessageConverter(messageConverter());
        log.debug("[RabbitMqConfig] RabbitTemplate 已配置 JSON 转换器");
        return template;
    }

    /**
     * 消费者监听容器 — prefetch=1 + MANUAL ack（spec §3.1, §8.3）。
     *
     * <p>手动 ack：业务成功才 ack；失败 nack(requeue=false) 进 DLQ。
     * 使用 {@code @RabbitListener} 的方法需声明 {@code Channel} 参数自行 ack。</p>
     */
    @Bean
    public RabbitListenerContainerFactory<?> rabbitListenerContainerFactory(ConnectionFactory connectionFactory) {
        SimpleRabbitListenerContainerFactory factory = new SimpleRabbitListenerContainerFactory();
        factory.setConnectionFactory(connectionFactory);
        factory.setMessageConverter(messageConverter());
        factory.setPrefetchCount(1); // spec §3.1
        factory.setAcknowledgeMode(AcknowledgeMode.MANUAL); // spec §8.3
        return factory;
    }
}
