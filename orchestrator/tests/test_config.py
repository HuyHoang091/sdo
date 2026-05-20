"""
Unit tests for configuration management.

Tests verify:
- YAML configuration loading from SDO_CONFIG
- Per-service configuration
- Environment variable overrides
- Configuration validation
- Sensible defaults

Requirements: 15.1, 15.2, 15.3, 15.4, 15.5
"""

import os
import pytest
import tempfile
import yaml
from unittest.mock import patch

# Import after setting up environment
def test_yaml_loading_from_sdo_config():
    """Test YAML configuration loading from SDO_CONFIG environment variable.
    
    Requirements: 15.1
    """
    # Create a temporary YAML file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
        config_data = {
            'services': {
                'test-service': {
                    'min_containers': 2,
                    'max_containers': 10,
                    'cpu_high': 85,
                    'cpu_low': 25,
                    'mem_danger': 95,
                    'mem_warning': 85,
                    'cooldown_seconds': 45,
                    'tier': 2,
                    'depends_on': ['db'],
                    'scalable': True,
                }
            }
        }
        yaml.dump(config_data, f)
        temp_path = f.name
    
    try:
        # Set SDO_CONFIG and reload config module
        with patch.dict(os.environ, {'SDO_CONFIG': temp_path}):
            # Need to reload the module to pick up the new config
            import importlib
            import orchestrator.config as config_module
            importlib.reload(config_module)
            
            # Verify the config was loaded
            assert 'test-service' in config_module.SERVICE_CONFIGS
            cfg = config_module.SERVICE_CONFIGS['test-service']
            assert cfg['min_containers'] == 2
            assert cfg['max_containers'] == 10
            assert cfg['cpu_high'] == 85
            assert cfg['cpu_low'] == 25
            assert cfg['tier'] == 2
            assert cfg['depends_on'] == ['db']
            assert cfg['scalable'] is True
    finally:
        # Clean up
        os.unlink(temp_path)


def test_per_service_configuration():
    """Test per-service configuration with different settings.
    
    Requirements: 15.2
    """
    from orchestrator.config import SERVICE_CONFIGS, get_service_config
    
    # Verify that different services can have different configs
    # (using existing configs from sdo.yml or defaults)
    
    # Test that get_service_config returns per-service settings
    if 'web-app' in SERVICE_CONFIGS:
        cfg = get_service_config('web-app')
        assert 'min_containers' in cfg
        assert 'max_containers' in cfg
        assert 'cpu_high' in cfg
        assert 'cpu_low' in cfg
        assert 'mem_danger' in cfg
        assert 'mem_warning' in cfg
        assert 'cooldown_seconds' in cfg
        assert 'rollback_window_seconds' in cfg
        assert 'rollback_error_threshold' in cfg
        assert 'rollback_latency_factor' in cfg
        assert 'rollback_action' in cfg


def test_environment_variable_overrides():
    """Test that environment variables override global settings.
    
    Requirements: 15.3
    """
    # Test various environment variable overrides
    test_env = {
        'SDO_LOG_LEVEL': 'DEBUG',
        'SDO_PROMETHEUS_URL': 'http://test:9090',
        'SDO_AGENT_INTERVAL': '15',
        'SDO_AGENT_MODE': 'dry-run',
        'SDO_METRICS_PORT': '9999',
        'SDO_HOST_CPU_CRITICAL': '85',
        'SDO_HOST_MEM_CRITICAL': '85',
        'SDO_ROLLBACK_WINDOW': '600',
        'SDO_ROLLBACK_ERROR_THRESHOLD': '0.2',
        'SDO_ROLLBACK_LATENCY_FACTOR': '3.0',
    }
    
    with patch.dict(os.environ, test_env, clear=False):
        # Reload config to pick up environment variables
        import importlib
        import orchestrator.config as config_module
        importlib.reload(config_module)
        
        # Verify overrides
        assert config_module.PROMETHEUS_URL == 'http://test:9090'
        assert config_module.AGENT_INTERVAL == 15
        assert config_module.AGENT_MODE == 'dry-run'
        assert config_module.AGENT_METRICS_PORT == 9999
        assert config_module.HOST_CPU_CRITICAL == 85
        assert config_module.HOST_MEM_CRITICAL == 85
        assert config_module.ROLLBACK_WINDOW_SECONDS == 600
        assert config_module.ROLLBACK_ERROR_THRESHOLD == 0.2
        assert config_module.ROLLBACK_LATENCY_FACTOR == 3.0


def test_configuration_validation_missing_required_keys():
    """Test that validation catches missing required keys.
    
    Requirements: 15.4
    """
    from orchestrator.config import _validate_service_configs, SERVICE_CONFIGS
    
    # Save original config
    original_configs = SERVICE_CONFIGS.copy()
    
    try:
        # Add invalid config missing required keys
        SERVICE_CONFIGS['invalid-service'] = {
            'min_containers': 1,
            # Missing max_containers, cpu_high, cpu_low, mem_danger, mem_warning
        }
        
        # Should raise ValueError
        with pytest.raises(ValueError, match="missing required keys"):
            _validate_service_configs()
    finally:
        # Restore original config
        SERVICE_CONFIGS.clear()
        SERVICE_CONFIGS.update(original_configs)


def test_configuration_validation_invalid_values():
    """Test that validation catches invalid values.
    
    Requirements: 15.4
    """
    from orchestrator.config import _validate_service_configs, SERVICE_CONFIGS
    
    # Save original config
    original_configs = SERVICE_CONFIGS.copy()
    
    try:
        # Test 1: min > max
        SERVICE_CONFIGS.clear()
        SERVICE_CONFIGS['invalid-service-1'] = {
            'min_containers': 10,
            'max_containers': 5,  # Less than min
            'cpu_high': 80,
            'cpu_low': 20,
            'mem_danger': 90,
            'mem_warning': 80,
        }
        
        with pytest.raises(ValueError, match="invalid min/max_containers"):
            _validate_service_configs()
        
        # Test 2: rollback_error_threshold out of range
        SERVICE_CONFIGS.clear()
        SERVICE_CONFIGS['invalid-service-2'] = {
            'min_containers': 1,
            'max_containers': 5,
            'cpu_high': 80,
            'cpu_low': 20,
            'mem_danger': 90,
            'mem_warning': 80,
            'rollback_error_threshold': 1.5,  # > 1.0
        }
        
        with pytest.raises(ValueError, match="rollback_error_threshold must be 0–1"):
            _validate_service_configs()
    finally:
        # Restore original config
        SERVICE_CONFIGS.clear()
        SERVICE_CONFIGS.update(original_configs)


def test_configuration_validation_unknown_keys():
    """Test that validation catches unknown keys.
    
    Requirements: 15.4
    """
    from orchestrator.config import _validate_service_configs, SERVICE_CONFIGS
    
    # Save original config
    original_configs = SERVICE_CONFIGS.copy()
    
    try:
        # Add config with unknown keys
        SERVICE_CONFIGS['invalid-service'] = {
            'min_containers': 1,
            'max_containers': 5,
            'cpu_high': 80,
            'cpu_low': 20,
            'mem_danger': 90,
            'mem_warning': 80,
            'unknown_key': 'value',  # Not in _VALID_KEYS
            'another_unknown': 123,
        }
        
        with pytest.raises(ValueError, match="has unknown keys"):
            _validate_service_configs()
    finally:
        # Restore original config
        SERVICE_CONFIGS.clear()
        SERVICE_CONFIGS.update(original_configs)


def test_sensible_defaults():
    """Test that sensible defaults are provided when config not specified.
    
    Requirements: 15.5
    """
    from orchestrator.config import (
        get_service_config, 
        ROLLBACK_WINDOW_SECONDS,
        ROLLBACK_ERROR_THRESHOLD,
        ROLLBACK_LATENCY_FACTOR,
        ROLLBACK_ACTION
    )
    
    # Get config for a service that doesn't exist in SERVICE_CONFIGS
    cfg = get_service_config('nonexistent-service')
    
    # Verify all required fields have defaults
    assert cfg['min_containers'] == 1
    assert cfg['max_containers'] == 10
    assert cfg['cpu_high'] == 80
    assert cfg['cpu_low'] == 20
    assert cfg['mem_danger'] == 90
    assert cfg['mem_warning'] == 80
    assert cfg['cooldown_seconds'] == 60
    assert cfg['rollback_window_seconds'] == ROLLBACK_WINDOW_SECONDS
    assert cfg['rollback_error_threshold'] == ROLLBACK_ERROR_THRESHOLD
    assert cfg['rollback_latency_factor'] == ROLLBACK_LATENCY_FACTOR
    assert cfg['rollback_action'] == ROLLBACK_ACTION
    assert cfg['rollback_min_observation'] == 90


def test_config_merge_with_defaults():
    """Test that service configs are properly merged with defaults.
    
    Requirements: 15.5
    """
    from orchestrator.config import (
        get_service_config, 
        SERVICE_CONFIGS, 
        ROLLBACK_WINDOW_SECONDS,
        ROLLBACK_ERROR_THRESHOLD
    )
    
    # Save original config
    original_configs = SERVICE_CONFIGS.copy()
    
    try:
        # Add a service with partial config
        SERVICE_CONFIGS['partial-service'] = {
            'min_containers': 2,
            'max_containers': 6,
            'cpu_high': 85,
            'cpu_low': 25,
            'mem_danger': 95,
            'mem_warning': 85,
            # Missing cooldown and rollback settings
        }
        
        cfg = get_service_config('partial-service')
        
        # Verify specified values are used
        assert cfg['min_containers'] == 2
        assert cfg['max_containers'] == 6
        assert cfg['cpu_high'] == 85
        
        # Verify defaults are used for missing values
        assert cfg['cooldown_seconds'] == 60  # Default
        assert cfg['rollback_window_seconds'] == ROLLBACK_WINDOW_SECONDS
        assert cfg['rollback_error_threshold'] == ROLLBACK_ERROR_THRESHOLD
    finally:
        # Restore original config
        SERVICE_CONFIGS.clear()
        SERVICE_CONFIGS.update(original_configs)


def test_yaml_file_not_found():
    """Test graceful handling when YAML file doesn't exist.
    
    Requirements: 15.1, 15.4
    """
    with patch.dict(os.environ, {'SDO_CONFIG': '/nonexistent/path/config.yml'}):
        # Should not crash, just log warning
        import importlib
        import orchestrator.config as config_module
        
        # This should not raise an exception
        importlib.reload(config_module)


def test_yaml_invalid_structure():
    """Test handling of invalid YAML structure.
    
    Requirements: 15.1, 15.4
    """
    # Create a temporary YAML file with invalid structure
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
        # services should be a dict, not a list
        f.write("services: [invalid, structure]\n")
        temp_path = f.name
    
    try:
        with patch.dict(os.environ, {'SDO_CONFIG': temp_path}):
            # Should not crash, just log warning
            import importlib
            import orchestrator.config as config_module
            importlib.reload(config_module)
    finally:
        os.unlink(temp_path)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
