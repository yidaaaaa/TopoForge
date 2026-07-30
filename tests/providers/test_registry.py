from topoforge.providers import list_provider_descriptors


def test_copernicus_aws_is_the_explicit_no_key_global_route() -> None:
    descriptors = {item.provider_id: item for item in list_provider_descriptors()}

    aws = descriptors["copernicus-aws"]
    assert aws.requires_api_key is False
    assert aws.implemented is False
    assert "GLO-30/GLO-90" in aws.name

    cdse = descriptors["copernicus-cdse"]
    assert cdse.requires_api_key is True
    assert cdse.implemented is False
