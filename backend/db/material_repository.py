"""Compatibility import for the normalized Course Library repository.

The task façade historically probes ``backend.db.material_repository``.  Keep
that narrow module name while the implementation lives in the more explicit
``course_library_repository`` module.
"""
from backend.db.course_library_repository import (  # noqa: F401
    CourseMaterial,
    CourseMaterialGroup,
    CourseRef,
    DeletedMaterial,
    DuplicateGroupName,
    GroupCourseMismatch,
    MaterialDocumentUnavailable,
    MaterialReferenced,
    create_group,
    create_material,
    delete_group,
    delete_material,
    find_group_by_normalized_name,
    get_group,
    get_material,
    get_material_by_document,
    get_owned_course,
    list_groups,
    list_materials,
    update_group,
    update_material,
)


__all__ = [
    "CourseMaterial",
    "CourseMaterialGroup",
    "CourseRef",
    "DeletedMaterial",
    "DuplicateGroupName",
    "GroupCourseMismatch",
    "MaterialDocumentUnavailable",
    "MaterialReferenced",
    "create_group",
    "create_material",
    "delete_group",
    "delete_material",
    "find_group_by_normalized_name",
    "get_group",
    "get_material",
    "get_material_by_document",
    "get_owned_course",
    "list_groups",
    "list_materials",
    "update_group",
    "update_material",
]
