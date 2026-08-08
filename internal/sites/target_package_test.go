package sites

import (
	"context"
	"testing"
)

type fakeTargetPackageAdapter struct{ code string }

func (adapter fakeTargetPackageAdapter) SiteCode() string { return adapter.code }
func (adapter fakeTargetPackageAdapter) PreparePackage(_ context.Context, material TargetPackageMaterial) (PreparedTargetPackage, error) {
	return PreparedTargetPackage{Target: material.Target}, nil
}

func TestTargetPackageRegistryRoutesByNormalizedSite(t *testing.T) {
	registry, err := NewTargetPackageRegistry(fakeTargetPackageAdapter{code: "mteam"})
	if err != nil {
		t.Fatal(err)
	}
	result, err := registry.PreparePackage(context.Background(), TargetPackageMaterial{Target: "mteam"})
	if err != nil || result.Target != "MTEAM" {
		t.Fatalf("PreparePackage() result/error = %#v/%v", result, err)
	}
	_, err = registry.PreparePackage(context.Background(), TargetPackageMaterial{Target: "TTG"})
	code, _, _ := ErrorDetails(err)
	if code != "target_package_adapter_unavailable" {
		t.Fatalf("unavailable target error = %q/%v", code, err)
	}
}
