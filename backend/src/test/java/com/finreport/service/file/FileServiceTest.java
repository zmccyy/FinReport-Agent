package com.finreport.service.file;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.LocalDateTime;
import java.util.Map;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;
import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.core.io.buffer.DataBufferFactory;
import org.springframework.core.io.buffer.DefaultDataBufferFactory;
import org.springframework.data.redis.core.ReactiveRedisTemplate;
import org.springframework.data.redis.core.ReactiveValueOperations;
import org.springframework.http.MediaType;
import org.springframework.http.codec.multipart.FilePart;

import com.finreport.domain.entity.Report;
import com.finreport.domain.entity.Task;
import com.finreport.domain.enums.TaskStatus;
import com.finreport.exception.BusinessException;
import com.finreport.repository.ReportRepository;
import com.finreport.service.orchestrator.TaskOrchestrator;

import io.minio.BucketExistsArgs;
import io.minio.MinioClient;
import io.minio.PutObjectArgs;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

/**
 * FileService 单元测试 — M1.12。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
@DisplayName("FileService")
class FileServiceTest {

    @Mock
    private MinioClient minioClient;

    @Mock
    private ReportRepository reportRepo;

    @Mock
    private TaskOrchestrator orchestrator;

    @Mock
    private ReactiveRedisTemplate<String, String> redisTemplate;

    @Mock
    private ReactiveValueOperations<String, String> valueOps;

    private FileService fileService;
    private final DataBufferFactory bufferFactory = new DefaultDataBufferFactory();

    // Sample PDF bytes (minimal valid PDF)
    private static final byte[] SAMPLE_PDF = "%PDF-1.4\n1 0 obj\n<<\n>>\nendobj\ntrailer\n<<\n>>\n%%EOF".getBytes();

    @BeforeEach
    void setUp() {
        fileService = new FileService(minioClient, reportRepo, orchestrator, redisTemplate);
        when(redisTemplate.opsForValue()).thenReturn(valueOps);
        // Default stubs — specific tests override as needed
        when(valueOps.get(anyString())).thenReturn(Mono.empty());
    }

    // ========================================================================
    // Helper: create a mock FilePart
    // ========================================================================

    private FilePart mockFilePart(String filename, MediaType contentType, byte[] content) {
        return mockFilePart(filename, contentType, Flux.just(bufferFactory.wrap(content)));
    }

    private FilePart mockFilePart(String filename, MediaType contentType, Flux<DataBuffer> content) {
        FilePart filePart = new FilePart() {
            @Override
            public String filename() {
                return filename;
            }

            @Override
            public Flux<DataBuffer> content() {
                return content;
            }

            @Override
            public Mono<Void> transferTo(java.io.File dest) {
                return Mono.empty();
            }

            @Override
            public Mono<Void> transferTo(java.nio.file.Path dest) {
                return Mono.empty();
            }

            @Override
            public String name() {
                return "file";
            }

            @Override
            public org.springframework.http.HttpHeaders headers() {
                org.springframework.http.HttpHeaders headers = new org.springframework.http.HttpHeaders();
                headers.setContentType(contentType);
                return headers;
            }
        };
        return filePart;
    }

    // ========================================================================
    // Nested: Successful upload
    // ========================================================================

    @Nested
    @DisplayName("upload")
    class Upload {

        @Test
        @DisplayName("should upload PDF and create task/report")
        void shouldUploadSuccessfully() throws Exception {
            FilePart filePart = mockFilePart("report.pdf", MediaType.APPLICATION_PDF, SAMPLE_PDF);

            // MinIO: bucket exists
            when(minioClient.bucketExists(any(BucketExistsArgs.class))).thenReturn(true);
            // Report: no MD5 duplicate
            when(reportRepo.findByPdfMd5(anyString())).thenReturn(Mono.empty());

            // Orchestrator: create task (without dispatch)
            Task task = Task.builder()
                    .id("task-abc123")
                    .userId(1L)
                    .taskType("REPORT_PARSE")
                    .status(TaskStatus.PENDING.name())
                    .progress(0)
                    .createdAt(LocalDateTime.now())
                    .build();
            when(orchestrator.createTaskWithoutDispatch(eq(1L), eq(null), any(Map.class)))
                    .thenReturn(Mono.just(task));

            // Orchestrator: update refReportId
            when(orchestrator.updateRefReportId(eq("task-abc123"), eq(99L)))
                    .thenAnswer(inv -> {
                        task.setRefReportId(99L);
                        return Mono.just(task);
                    });

            // Orchestrator: dispatch PARSE after MinIO upload
            when(orchestrator.dispatchTask(eq("task-abc123"), any(Map.class)))
                    .thenReturn(Mono.just(task));

            // Report: save
            when(reportRepo.save(any(Report.class))).thenAnswer(inv -> {
                Report r = inv.getArgument(0);
                r.setId(99L);
                return Mono.just(r);
            });

            // Redis idempotency cache set
            when(valueOps.set(anyString(), anyString(), any(java.time.Duration.class)))
                    .thenReturn(Mono.just(true));

            StepVerifier.create(fileService.upload(filePart, "600519", "贵州茅台", "ANNUAL",
                            "2024-12-31", 1L, "idem-key-001"))
                    .assertNext(response -> {
                        assertEquals("task-abc123", response.taskId());
                        assertEquals(99L, response.reportId());
                        assertEquals("PENDING", response.status());
                    })
                    .verifyComplete();

            ArgumentCaptor<PutObjectArgs> putObjectCaptor = ArgumentCaptor.forClass(PutObjectArgs.class);
            verify(minioClient).putObject(putObjectCaptor.capture());
            assertEquals(SAMPLE_PDF.length, putObjectCaptor.getValue().objectSize());
            verify(reportRepo).save(any(Report.class));
            verify(orchestrator).createTaskWithoutDispatch(eq(1L), eq(null), any(Map.class));
            verify(orchestrator).dispatchTask(eq("task-abc123"), any(Map.class));
        }

        @Test
        @DisplayName("should reuse existing report on MD5 match")
        void shouldReuseReportOnMd5Match() throws Exception {
            FilePart filePart = mockFilePart("report.pdf", MediaType.APPLICATION_PDF, SAMPLE_PDF);

            // Existing report with matching MD5
            Report existingReport = Report.builder()
                    .id(55L).taskId("task-old").userId(1L)
                    .companyCode("600519").companyName("贵州茅台")
                    .reportType("ANNUAL").reportPeriod("2024-12-31")
                    .pdfMd5(FileService.computeMd5(SAMPLE_PDF))
                    .pdfObjectKey("uploads/1/202607/task-old/report.pdf")
                    .parseStatus("PENDING").build();
            when(reportRepo.findByPdfMd5(anyString())).thenReturn(Mono.just(existingReport));

            // New task for existing report
            Task newTask = Task.builder()
                    .id("task-new01")
                    .userId(1L)
                    .refReportId(55L)
                    .status(TaskStatus.PARSE_RUNNING.name())
                    .build();
            when(orchestrator.createTask(eq(1L), eq(55L), any(Map.class)))
                    .thenReturn(Mono.just(newTask));

            // Redis
            when(valueOps.set(anyString(), anyString(), any(java.time.Duration.class)))
                    .thenReturn(Mono.just(true));

            StepVerifier.create(fileService.upload(filePart, "600519", "贵州茅台", "ANNUAL",
                            "2024-12-31", 1L, "idem-key-002"))
                    .assertNext(response -> {
                        assertEquals("task-new01", response.taskId());
                        assertEquals(55L, response.reportId());
                    })
                    .verifyComplete();

            // Should NOT upload to MinIO
            verify(minioClient, never()).putObject(any(PutObjectArgs.class));
            // Should NOT create new report
            verify(reportRepo, never()).save(any(Report.class));
        }
    }

    // ========================================================================
    // Nested: Idempotency
    // ========================================================================

    @Nested
    @DisplayName("idempotency")
    class Idempotency {

        @Test
        @DisplayName("should return cached result on duplicate Idempotency-Key")
        void shouldReturnCachedResult() {
            FilePart filePart = mockFilePart("report.pdf", MediaType.APPLICATION_PDF, SAMPLE_PDF);

            String cached = "{\"taskId\":\"task-cached\",\"reportId\":42,\"status\":\"PENDING\"}";
            when(valueOps.get("fin:idem:upload:idem-key-003")).thenReturn(Mono.just(cached));

            StepVerifier.create(fileService.upload(filePart, "600519", "茅台", "ANNUAL",
                            "2024-12-31", 1L, "idem-key-003"))
                    .assertNext(response -> {
                        assertEquals("task-cached", response.taskId());
                        assertEquals(42L, response.reportId());
                        assertEquals("PENDING", response.status());
                    })
                    .verifyComplete();

            // Should NOT process the file
            verify(reportRepo, never()).findByPdfMd5(anyString());
            verify(orchestrator, never()).createTaskWithoutDispatch(any(), any(), any(Map.class));
        }

        @Test
        @DisplayName("should proceed normally when no Idempotency-Key")
        void shouldProceedWithoutIdempotencyKey() throws Exception {
            FilePart filePart = mockFilePart("report.pdf", MediaType.APPLICATION_PDF, SAMPLE_PDF);

            when(minioClient.bucketExists(any(BucketExistsArgs.class))).thenReturn(true);
            when(reportRepo.findByPdfMd5(anyString())).thenReturn(Mono.empty());

            Task task = Task.builder()
                    .id("task-no-idem").userId(1L)
                    .taskType("REPORT_PARSE")
                    .status(TaskStatus.PENDING.name())
                    .createdAt(LocalDateTime.now()).build();
            when(orchestrator.createTaskWithoutDispatch(eq(1L), eq(null), any(Map.class)))
                    .thenReturn(Mono.just(task));

            when(orchestrator.updateRefReportId(eq("task-no-idem"), any()))
                    .thenAnswer(inv -> {
                        task.setRefReportId(10L);
                        return Mono.just(task);
                    });

            when(orchestrator.dispatchTask(eq("task-no-idem"), any(Map.class)))
                    .thenReturn(Mono.just(task));

            when(reportRepo.save(any(Report.class))).thenAnswer(inv -> {
                Report r = inv.getArgument(0);
                r.setId(10L);
                return Mono.just(r);
            });

            // No idempotency key → no Redis cache
            StepVerifier.create(fileService.upload(filePart, "600519", "茅台", "ANNUAL",
                            "2024-12-31", 1L, null))
                    .assertNext(response -> {
                        assertEquals("task-no-idem", response.taskId());
                    })
                    .verifyComplete();

            // Redis set should NOT be called (no key to cache)
            verify(valueOps, never()).set(anyString(), anyString(), any());
        }
    }

    // ========================================================================
    // Nested: Validation errors
    // ========================================================================

    @Nested
    @DisplayName("validation")
    class Validation {

        @Test
        @DisplayName("should reject non-PDF files")
        void shouldRejectNonPdfFile() {
            FilePart filePart = mockFilePart("report.txt", MediaType.TEXT_PLAIN, "hello".getBytes());

            StepVerifier.create(fileService.upload(filePart, "600519", "茅台", "ANNUAL",
                            "2024-12-31", 1L, null))
                    .expectErrorMatches(ex -> ex instanceof BusinessException
                            && "INVALID_FILE_TYPE".equals(((BusinessException) ex).getErrorCode()))
                    .verify();
        }

        @Test
        @DisplayName("should reject streamed files exceeding 50MB")
        void shouldRejectStreamedFilesExceeding50Mb() throws Exception {
            int chunkSize = 1024 * 1024;
            FilePart filePart = mockFilePart("large.pdf", MediaType.APPLICATION_PDF,
                    Flux.range(0, 51).map(index -> bufferFactory.wrap(new byte[chunkSize])));

            StepVerifier.create(fileService.upload(filePart, "600519", "茅台", "ANNUAL",
                            "2024-12-31", 1L, null))
                    .expectErrorMatches(ex -> ex instanceof BusinessException
                            && "FILE_TOO_LARGE".equals(((BusinessException) ex).getErrorCode()))
                    .verify();

            verify(orchestrator, never()).createTaskWithoutDispatch(any(), any(), any());
            verify(minioClient, never()).putObject(any(PutObjectArgs.class));
        }
    }

    // ========================================================================
    // Nested: Static helpers
    // ========================================================================

    @Nested
    @DisplayName("computeMd5")
    class ComputeMd5 {

        @Test
        @DisplayName("should compute correct MD5")
        void shouldComputeCorrectMd5() {
            byte[] data = "hello world".getBytes();
            String md5 = FileService.computeMd5(data);
            assertNotNull(md5);
            assertEquals(32, md5.length());
            // "hello world" → 5eb63bbbe01eeed093cb22bb8f5acdc3
            assertEquals("5eb63bbbe01eeed093cb22bb8f5acdc3", md5);
        }

        @Test
        @DisplayName("should produce different MD5 for different inputs")
        void shouldDifferentiate() {
            String md5a = FileService.computeMd5("abc".getBytes());
            String md5b = FileService.computeMd5("abd".getBytes());
            assertTrue(!md5a.equals(md5b));
        }
    }

    @Nested
    @DisplayName("buildObjectKey")
    class BuildObjectKey {

        @Test
        @DisplayName("should follow spec §5.5.2 format")
        void shouldFollowSpecFormat() {
            String key = FileService.buildObjectKey(1L, "task-abc123", "report.pdf");
            assertTrue(key.startsWith("uploads/1/"));
            assertTrue(key.contains("/task-abc123/"));
            assertTrue(key.endsWith("report.pdf"));
            // Verify yyyyMM pattern
            String[] parts = key.split("/");
            String yyyyMM = parts[2];
            assertEquals(6, yyyyMM.length());
        }
    }
}
