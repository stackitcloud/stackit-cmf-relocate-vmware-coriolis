package cloud.scf.relocate;

import java.util.Map;
import java.util.UUID;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class EvidenceController {
    private final JdbcTemplate jdbc;

    public EvidenceController(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @GetMapping("/api/evidence")
    public Map<String, Object> evidence() {
        return jdbc.queryForMap("""
            SELECT count(*) AS records,
                   count(*) FILTER (WHERE kind = 'seed') AS seed_records,
                   count(*) FILTER (WHERE kind = 'write') AS writes,
                   md5(string_agg(record_id || ':' || payload, E'\n' ORDER BY record_id)) AS digest
            FROM migration_records
            """);
    }

    @PostMapping("/api/writes")
    public Map<String, Object> write() {
        String id = UUID.randomUUID().toString();
        return jdbc.queryForMap("""
            INSERT INTO migration_records(record_id, kind, payload)
            VALUES (?, 'write', ?) RETURNING record_id, created_at
            """, id, "synthetic-" + id);
    }
}
