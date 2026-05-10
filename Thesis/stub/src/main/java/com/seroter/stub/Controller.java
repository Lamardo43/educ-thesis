package com.seroter.stub;

import io.micrometer.core.annotation.Counted;
import io.micrometer.core.annotation.Timed;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.UUID;

@RestController
public class Controller {

    @Counted
    @Timed
    @GetMapping("/api/uuid")
    public String getUuid() {
        return UUID.randomUUID().toString();
    }
}
